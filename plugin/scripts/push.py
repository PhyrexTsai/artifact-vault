"""Stop / SessionStart hook: move queued versions from the spool into each vault and push.

For every vault in ${CLAUDE_PLUGIN_DATA}/spool/<key>/:
  1. Take an OS file lock. If another session holds it, stop: the next turn tries again.
  2. Clone the vault the first time, then make the clone exactly the remote branch (fetch and
     reset). Git never prompts (GIT_TERMINAL_PROMPT=0, ssh BatchMode), so bad credentials fail
     instead of hanging.
  3. Copy each version into pages/<id>/<version>/ (its meta.json included). Push never
     edits a shared file: every version has its own path, so pushes from several machines
     never conflict. The per-artifact summary and duplicate hiding (same digest
     as the previous seq) happen when the site is built. Git stores identical content once,
     so a duplicate version costs almost nothing.
  4. Commit pages/ (forced past .gitignore), check that every file landed in the commit, and
     push. Only a pushed version leaves the spool. A failed push undoes the commit; the
     versions stay queued and the run starts over from the remote (a few times), so the
     clone never holds unpushed work and never rebases.

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
import time

GIT_TIMEOUT = 120
CLONE_TIMEOUT = 300


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def git_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return env


NO_HOOKS = ["-c", f"core.hooksPath={os.devnull}"]  # see configure(): hooks never run in the vault clone


def git(*args, cwd=None, timeout=GIT_TIMEOUT, inp=None):
    r = subprocess.run(["git", *NO_HOOKS, *args], cwd=cwd, env=git_env(), capture_output=True, text=True,
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
    """Clone the vault the first time."""
    if os.path.isdir(os.path.join(clone, ".git")):
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


def rev(clone, name):
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", name], cwd=clone, capture_output=True, text=True, env=git_env())
    return r.stdout.strip() or None


def mirror_remote(clone):
    """Make the clone exactly the remote branch.

    This clone never holds work of its own: a run commits its versions and pushes them, or
    undoes the commit and leaves them in the spool. So whatever is here can be thrown away,
    and a remote whose history was rewritten (a purge) is followed exactly like one that
    moved forward. Nothing is ever rebased."""
    branch = current_branch(clone)
    git("fetch", "--quiet", "--prune", "origin", cwd=clone)
    remote = rev(clone, f"refs/remotes/origin/{branch}")
    if remote:
        git("reset", "-q", "--hard", remote, cwd=clone)
    elif head(clone):  # the vault is still empty: drop a commit an interrupted run left
        undo_commit(clone, None)
    else:
        git("rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", ".", cwd=clone)
    git("clean", "-q", "-f", "-d", "-x", cwd=clone)  # untracked leftovers anywhere (pages, overrides)


def configure(clone):
    # Store file names byte for byte. macOS git otherwise precomposes Unicode names, and the
    # archived HTML and meta.json would reference a spelling the commit does not have.
    git("config", "core.precomposeunicode", "false", cwd=clone)
    # This clone belongs to the plugin and only stores archives. Your global git hooks
    # (formatters, checks) would rewrite archived pages here, so they do not run in it: every
    # git call also passes NO_HOOKS, which wins over environment settings. Your own
    # repositories are not affected.
    git("config", "core.hooksPath", os.devnull, cwd=clone)
    # Commits need an identity. Someone who set one only inside their project repos has none
    # here, so fall back to the plugin's author_email, in this clone only.
    author = os.environ.get("CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL", "").strip()
    if author:
        for key, value in (("user.email", author), ("user.name", author.split("@")[0])):
            missing = subprocess.run(["git", "config", key], cwd=clone, capture_output=True, env=git_env()).returncode
            if missing:  # fill each part on its own; keep whatever is already set
                git("config", key, value, cwd=clone)
    # Archive bytes as written: no end-of-line conversion, filters, or encoding changes from
    # the vault's own .gitattributes, for pages and overrides alike. info/attributes wins.
    attrs = os.path.join(clone, ".git", "info", "attributes")
    os.makedirs(os.path.dirname(attrs), exist_ok=True)
    rule = "* -text -filter -ident -working-tree-encoding\n"  # the whole private clone
    current = open(attrs).read() if os.path.exists(attrs) else ""
    if rule.strip() not in (line.strip() for line in current.splitlines()):  # whole lines: old rules are prefixes
        with open(attrs, "a") as fh:
            fh.write(rule)


def current_branch(clone):
    return git("symbolic-ref", "--short", "HEAD", cwd=clone).strip()


def head(clone):
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=clone, capture_output=True, text=True)
    return r.stdout.strip() or None


def check_commit(clone, before, expected):
    """The new commit must change exactly the expected files, with exactly the captured bytes.

    Anything else means a git hook or filter touched the archive (this round's files or
    older versions), so the commit cannot be trusted.
    """
    unchanged = head(clone) == before  # e.g. a run killed after its commit, before retire()
    git("checkout", "-q", "HEAD", "--", "pages", cwd=clone)  # drop rewrites a hook left unstaged
    committed = {}  # -z: git would otherwise quote non-ASCII paths such as Chinese names
    for entry in git("ls-tree", "-r", "-z", "HEAD", cwd=clone).split("\0"):
        if "\t" in entry:
            info, path = entry.split("\t", 1)
            committed[path] = info.split()[2]
    if any(committed.get(p) != sha for p, sha in expected.items()):
        return "archived files missing or changed in the commit"
    if unchanged:
        return None  # already committed with the exact bytes: just retire the spool
    diff = ["diff", "--name-only", "-z", "--no-renames", before, "HEAD", "--"] if before else \
        ["ls-tree", "-r", "-z", "--name-only", "HEAD", "--"]
    touched = {p for p in git(*diff, cwd=clone).split("\0") if p}
    if touched - set(expected):
        return "the commit changed files outside this round's versions"
    return None


def undo_commit(clone, before):
    if before:
        git("reset", "-q", "--hard", before, cwd=clone)
    else:
        git("update-ref", "-d", "HEAD", cwd=clone)
        git("rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", ".", cwd=clone)


def commit_pages(clone, subject):
    """Stage pages/ past any .gitignore and commit if anything is staged."""
    git("add", "-f", "--", "pages", cwd=clone)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "pages"], cwd=clone, env=git_env())
    if staged.returncode:
        git("commit", "--quiet", "-m", subject, cwd=clone)


def retire(folder, trash):
    """Leave the queue atomically, then delete. The rename moves the folder out of the spool
    in one step, so an interrupted delete can never leave a partial folder that still looks
    like a complete version."""
    os.makedirs(trash, exist_ok=True)
    gone = os.path.join(trash, f"{os.getpid()}-{time.time_ns()}")
    os.replace(folder, gone)
    shutil.rmtree(gone, ignore_errors=True)


def blob_id(path, algo="sha1"):
    """Git's id for a file's bytes, computed without asking git, so no file name is ever
    parsed by a git command. algo follows the repo's object format (sha1 or sha256)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.new(algo, b"blob %d\0" % len(data) + data).hexdigest()


def inside_clone(clone, dest):
    """Refuse to write through a symlink: a vault whose pages/ (or an artifact folder) links
    elsewhere would otherwise make rmtree/copytree touch files outside the clone."""
    root = os.path.realpath(clone)
    path = root
    for part in os.path.relpath(dest, clone).split(os.sep):
        path = os.path.join(path, part)
        if os.path.islink(path):
            raise RuntimeError("the vault has a symlink under pages/; refusing to write through it")
    if os.path.commonpath([root, os.path.realpath(dest)]) != root:
        raise RuntimeError("archive path resolves outside the vault clone")


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
    inside_clone(clone, dest)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(folder, dest)
    return {f"{rel}/{src}": blob for src, blob in zip(sources, blobs)}


def refresh_clone(data_dir, key, url, clone):
    """Bring the private clone to the remote's state. Call with the lock held. Every user of
    the clone (push, the page, tag, backfill and purge skills) goes through here."""
    ensure_clone(url, clone)
    configure(clone)
    mirror_remote(clone)


def purged_ids(clone):
    """Pages removed with the purge skill: purged/<id>.json in the vault."""
    folder = os.path.join(clone, "purged")
    if not os.path.isdir(folder) or os.path.islink(folder):
        return set()
    return {n[:-5] for n in os.listdir(folder) if n.endswith(".json")}


MAX_TRIES = 3


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
        trash = os.path.join(data_dir, "trash")
        shutil.rmtree(trash, ignore_errors=True)  # leftovers of an interrupted retire()
        dropped = 0
        for attempt in range(MAX_TRIES):
            refresh_clone(data_dir, key, url, clone)
            purged = purged_ids(clone)
            for meta, folder in queued_versions(spool):  # a purged page never comes back
                if meta["id"] in purged:
                    retire(folder, trash)
                    dropped += 1
            extra = {"dropped_purged": dropped} if dropped else {}
            queued = queued_versions(spool)
            if not queued:
                return {"applied": 0, "pushed": 0, **extra}
            expected, titles = {}, []
            for meta, folder in queued:
                expected.update(apply_version(clone, meta, folder))
                titles.append(meta.get("title") or meta["id"])
            before = head(clone)
            commit_pages(clone, f"archive: {titles[0]}" if len(titles) == 1 else f"archive: {len(titles)} versions")
            problem = check_commit(clone, before, expected)
            if problem:
                undo_commit(clone, before)
                raise RuntimeError(f"{problem}; commit undone, spool kept")
            changed = head(clone) != before  # unchanged: already pushed by a run that died before retiring
            if changed:
                try:
                    push_head(clone)
                except RuntimeError as e:
                    undo_commit(clone, before)
                    if attempt == MAX_TRIES - 1:
                        raise RuntimeError(f"{e}; the versions stay queued")
                    continue  # someone pushed (or purged) first: start over from the remote
            for _, folder in queued:  # only now: the remote holds their content
                retire(folder, trash)
            return {"applied": len(queued), "pushed": int(changed), **extra}


def check_purged(clone):
    """Never push a purged page."""
    back = [a for a in purged_ids(clone)
            if git("ls-tree", "--name-only", "HEAD", "--", f"pages/{a}", f"overrides/{a}", cwd=clone).strip()]
    if back:
        raise RuntimeError(f"{len(back)} purged page(s) are in this commit; not pushing")


def push_head(clone):
    """Push the clone's commit as the remote branch. Fast-forward only: a rejected push means
    the remote moved, and the caller undoes its commit and starts over from the remote."""
    check_purged(clone)
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{current_branch(clone)}", cwd=clone)


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
    # Hooks get CLAUDE_PLUGIN_DATA in the environment; a skill running this through the Bash
    # tool does not, so it passes --data <dir>.
    if len(sys.argv) >= 3 and sys.argv[1] == "--data" and sys.argv[2] and "${" not in sys.argv[2]:
        os.environ["CLAUDE_PLUGIN_DATA"] = sys.argv[2]
    try:
        main()
    except Exception as e:
        print(f"artifact-vault: push failed: {e}", file=sys.stderr)
    sys.exit(0)
