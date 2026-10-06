"""Stop / SessionStart hook: move queued versions from the spool into each vault and push.

For every vault in ${CLAUDE_PLUGIN_DATA}/spool/<key>/:
  1. Take an OS file lock. If another session holds it, stop: the next turn tries again.
  2. Clone the vault the first time, or `pull --rebase` an existing clone. Git never prompts
     (GIT_TERMINAL_PROMPT=0, ssh BatchMode), so bad credentials fail instead of hanging.
  3. Copy each version into pages/<id>/<version>/ (its meta.json included). Push never
     edits a shared file: every version has its own path, so pushes from several machines
     rebase without conflicts. The per-artifact summary and duplicate hiding (same digest
     as the previous seq) happen when the site is built. Git stores identical content once,
     so a duplicate version costs almost nothing.
  4. Commit pages/ (forced past .gitignore), check that every file landed in the commit,
     then delete those spool folders and push. A failed push stays committed in the clone
     and is pushed by the next run.

Writes the outcome to state/<key>-push.json for the setup skill and appends to logs/push.log.
Never fails the session: errors are recorded and the hook exits 0.
"""
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys

GIT_TIMEOUT = 120
CLONE_TIMEOUT = 300


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def git_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return env


def git(*args, cwd=None, timeout=GIT_TIMEOUT, inp=None):
    r = subprocess.run(["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True,
                       timeout=timeout, input=inp)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


class Lock:
    """OS file lock (fcntl.flock). The kernel releases it when the process dies, so a killed
    run never leaves a stale lock and two runs can never both hold it."""

    def __init__(self, path):
        self.path, self.held, self.fh = path, False, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        except OSError:
            self.fh.close()
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def queued_versions(spool):
    """[(meta, folder)] for complete version folders, oldest seq first."""
    found = []
    for art in sorted(os.listdir(spool)):
        art_dir = os.path.join(spool, art)
        if not os.path.isdir(art_dir):
            continue
        for ver in sorted(os.listdir(art_dir)):
            folder = os.path.join(art_dir, ver)
            meta = read_json(os.path.join(folder, "meta.json"), None)
            if not isinstance(meta, dict) or meta.get("id") != art or meta.get("version") != ver:
                continue  # a temp folder of a running capture, or not a version at all
            found.append((meta, folder))
    found.sort(key=lambda mf: (mf[0].get("seq") or 0, mf[0].get("captured_at") or ""))
    return found


def ensure_clone(url, clone):
    if os.path.isdir(os.path.join(clone, ".git")):
        if not git("ls-remote", "--heads", "origin", cwd=clone).strip():
            return  # the vault has no branch yet: the first push creates it
        try:
            git("pull", "--rebase", "--quiet", "origin", current_branch(clone), cwd=clone)
        except RuntimeError:
            subprocess.run(["git", "rebase", "--abort"], cwd=clone, capture_output=True)
            raise
        return
    os.makedirs(os.path.dirname(clone), exist_ok=True)
    tmp = f"{clone}.cloning{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    # No checkout until configure() has turned off content conversion, or the vault's own
    # .gitattributes could rewrite archived files on the way out.
    git("clone", "--quiet", "--no-checkout", url, tmp, timeout=CLONE_TIMEOUT)
    configure(tmp)
    if subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=tmp, capture_output=True).returncode == 0:
        git("checkout", "--quiet", cwd=tmp)
    os.replace(tmp, clone)


def configure(clone):
    # Store file names byte for byte. macOS git otherwise precomposes Unicode names, and the
    # archived HTML and meta.json would reference a spelling the commit does not have.
    git("config", "core.precomposeunicode", "false", cwd=clone)
    # Archive bytes as captured: no end-of-line conversion or filters from the vault's own
    # .gitattributes. info/attributes takes precedence over it.
    attrs = os.path.join(clone, ".git", "info", "attributes")
    os.makedirs(os.path.dirname(attrs), exist_ok=True)
    rule = "pages/** -text -filter -ident -working-tree-encoding\n"
    current = open(attrs).read() if os.path.exists(attrs) else ""
    if rule not in current:
        with open(attrs, "a") as fh:
            fh.write(rule)


def current_branch(clone):
    return git("symbolic-ref", "--short", "HEAD", cwd=clone).strip()


def commit_pages(clone, subject):
    """Stage pages/ past any .gitignore and commit if anything is staged."""
    git("add", "-f", "--", "pages", cwd=clone)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "pages"], cwd=clone, env=git_env())
    if staged.returncode:
        git("commit", "--quiet", "-m", subject, cwd=clone)


def blob_id(path, algo="sha1"):
    """Git's id for a file's bytes, computed without asking git, so no file name is ever
    parsed by a git command. algo follows the repo's object format (sha1 or sha256)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.new(algo, b"blob %d\0" % len(data) + data).hexdigest()


def apply_version(clone, meta, folder):
    """Copy one version into pages/<id>/<version>/.

    Return {repo path: blob id} computed from the spool copy, before any git step can touch
    the bytes, so the commit can be checked against what was captured.
    """
    rel = "/".join(("pages", meta["id"], meta["version"]))
    sources = []
    for d, _, names in os.walk(folder):
        for n in names:
            sources.append(os.path.relpath(os.path.join(d, n), folder).replace(os.sep, "/"))
    algo = git("rev-parse", "--show-object-format", cwd=clone).strip() or "sha1"
    blobs = [blob_id(os.path.join(folder, *src.split("/")), algo) for src in sources]
    dest = os.path.join(clone, *rel.split("/"))
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(folder, dest)
    return {f"{rel}/{src}": blob for src, blob in zip(sources, blobs)}


def recover(clone):
    """Throw away uncommitted pages/ changes an earlier run left behind.

    The spool is deleted only after a commit that matches it, so anything uncommitted is
    either still in the spool (and is applied again) or was never verified (a crashed run,
    a hook that rewrote files). Committed but unpushed work is kept.
    """
    has_head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=clone, capture_output=True).returncode == 0
    if has_head and git("ls-tree", "HEAD", "--", "pages", cwd=clone).strip():
        git("restore", "-q", "--source=HEAD", "--staged", "--worktree", "--", "pages", cwd=clone)
    else:
        git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", "pages", cwd=clone)
    git("clean", "-q", "-f", "-d", "-x", "--", "pages", cwd=clone)


def push_vault(data_dir, key):
    spool = os.path.join(data_dir, "spool", key)
    target = read_json(os.path.join(spool, "vault.json"), {})
    url = target.get("vault")
    if not url:
        # capture writes vault.json with every version; a spool without it was never written
        # by a released capture, so there is no route to recover here.
        raise RuntimeError("spool has no vault.json")
    clone = os.path.join(data_dir, "vaults", key)
    with Lock(os.path.join(data_dir, "locks", f"{key}.lock")) as lock:
        if not lock.held:
            return {"skipped": "another session is pushing"}
        if os.path.isdir(os.path.join(clone, ".git")):
            recover(clone)  # before pull: a dirty tree would stop the rebase
        ensure_clone(url, clone)
        configure(clone)
        queued = queued_versions(spool)
        expected, titles = {}, []
        for meta, folder in queued:
            expected.update(apply_version(clone, meta, folder))
            titles.append(meta.get("title") or meta["id"])
        if expected:
            commit_pages(clone, f"archive: {titles[0]}" if len(titles) == 1 else f"archive: {len(titles)} versions")
            git("checkout", "-q", "HEAD", "--", "pages", cwd=clone)  # undo any rewrite a hook left unstaged
            # -z: git would otherwise quote and escape non-ASCII paths such as Chinese file names
            committed = {}
            for entry in git("ls-tree", "-r", "-z", "HEAD", "--", "pages", cwd=clone).split("\0"):
                if "\t" in entry:
                    info, path = entry.split("\t", 1)
                    committed[path] = info.split()[2]
            bad = [p for p, sha in expected.items() if committed.get(p) != sha]
            if bad:
                raise RuntimeError(f"{len(bad)} archived file(s) missing or changed in the commit; spool kept")
        for _, folder in queued:  # only now: the commit holds their content
            shutil.rmtree(folder, ignore_errors=True)
        ahead = commits_to_push(clone)
        if ahead:
            push(clone)
        return {"applied": len(queued), "pushed": ahead}


def commits_to_push(clone):
    ok = lambda *a: subprocess.run(["git", *a], cwd=clone, capture_output=True, text=True, env=git_env())
    if ok("rev-parse", "--verify", "HEAD").returncode:
        return 0  # empty clone, nothing committed yet
    r = ok("rev-list", "--count", "@{upstream}..HEAD")
    if r.returncode:  # no upstream yet (first push to an empty vault)
        r = ok("rev-list", "--count", "HEAD")
    return int(r.stdout.strip() or 0)


def push(clone):
    try:
        git("push", "--quiet", "-u", "origin", "HEAD", cwd=clone)
    except RuntimeError:
        if not git("ls-remote", "--heads", "origin", cwd=clone).strip():
            raise  # empty vault that rejected the first push: nothing to rebase onto
        git("pull", "--rebase", "--quiet", "origin", current_branch(clone), cwd=clone)  # someone pushed first
        git("push", "--quiet", "-u", "origin", "HEAD", cwd=clone)


def main(env=os.environ):
    data_dir = env.get("CLAUDE_PLUGIN_DATA")
    if not data_dir or not os.path.isdir(os.path.join(data_dir, "spool")):
        return 0
    log = os.path.join(data_dir, "logs", "push.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    for key in sorted(os.listdir(os.path.join(data_dir, "spool"))):
        if not os.path.isdir(os.path.join(data_dir, "spool", key)):
            continue
        try:
            result = {"ok": True, **push_vault(data_dir, key)}
        except Exception as e:  # never break the session
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
        spool = os.path.join(data_dir, "spool", key)
        result["pending"] = len(queued_versions(spool)) if os.path.isdir(spool) else 0
        result["at"] = now()
        if "skipped" not in result:
            write_json(os.path.join(data_dir, "state", f"{key}-push.json"), result)
        with open(log, "a") as fh:
            fh.write(json.dumps({"vault": key, **result}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"artifact-vault: push failed: {e}", file=sys.stderr)
    sys.exit(0)
