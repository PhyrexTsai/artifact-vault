"""Stop / SessionStart hook: move queued versions from the spool into each vault and push.

For every vault in ${CLAUDE_PLUGIN_DATA}/spool/<key>/:
  1. Take a lock (mkdir; macOS has no flock). If another session holds it, stop: the next
     turn tries again.
  2. Clone the vault the first time, or `pull --rebase` an existing clone. Git never prompts
     (GIT_TERMINAL_PROMPT=0, ssh BatchMode), so bad credentials fail instead of hanging.
  3. Apply versions in seq order into pages/<id>/<version>/ and update meta/<id>.json.
     A version whose digest equals the previous version (by seq) is dropped as a duplicate.
  4. Commit pages/ and meta/ only, delete the applied spool folders, and push. A failed push
     leaves the commit in the clone; the next run pushes it.

Writes the outcome to state/<key>-push.json for the setup skill and appends to logs/push.log.
Never fails the session: errors are recorded and the hook exits 0.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

GIT_TIMEOUT = 120
CLONE_TIMEOUT = 300
LOCK_STALE = 15 * 60


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def git_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return env


def git(*args, cwd=None, timeout=GIT_TIMEOUT):
    r = subprocess.run(["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


class Lock:
    def __init__(self, path):
        self.path, self.held = path, False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            os.mkdir(self.path)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(self.path) > LOCK_STALE:
                    os.rmdir(self.path)  # left behind by a killed run
                    os.mkdir(self.path)
                else:
                    return self
            except OSError:
                return self
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                os.rmdir(self.path)
            except OSError:
                pass


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
            if ".tmp" in ver or not isinstance(meta, dict) or meta.get("id") != art:
                continue  # half-written by a running capture
            found.append((meta, folder))
    found.sort(key=lambda mf: (mf[0].get("seq") or 0, mf[0].get("captured_at") or ""))
    return found


def ensure_clone(url, clone):
    if os.path.isdir(os.path.join(clone, ".git")):
        try:
            git("pull", "--rebase", "--quiet", cwd=clone)
        except RuntimeError as e:
            if "no tracking information" in str(e) or "couldn't find remote ref" in str(e):
                return  # empty remote: nothing to pull yet
            subprocess.run(["git", "rebase", "--abort"], cwd=clone, capture_output=True)
            raise
        return
    os.makedirs(os.path.dirname(clone), exist_ok=True)
    tmp = f"{clone}.cloning{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    git("clone", "--quiet", url, tmp, timeout=CLONE_TIMEOUT)
    os.replace(tmp, clone)


ARTIFACT_KEYS = ("id", "url", "title", "type", "repo", "author", "audience", "capabilities")
VERSION_KEYS = ("version", "seq", "captured_at", "digest", "title", "type", "files_written",
                "files_removed", "files_remote", "files_incomplete", "agent_type")


def apply_version(clone, meta, folder):
    """Copy one version into the clone. Return False when it duplicates the previous seq."""
    art = meta["id"]
    meta_path = os.path.join(clone, "meta", f"{art}.json")
    record = read_json(meta_path, {"id": art, "versions": []})
    versions = record.get("versions", [])
    if any(v.get("version") == meta["version"] for v in versions):
        return False  # already archived (a retried run)
    earlier = [v for v in versions if (v.get("seq") or 0) < (meta.get("seq") or 0)]
    if earlier and max(earlier, key=lambda v: v.get("seq") or 0).get("digest") == meta.get("digest"):
        return False  # same content as the version just before it
    dest = os.path.join(clone, "pages", art, meta["version"])
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(folder, dest)
    versions.append({k: meta.get(k) for k in VERSION_KEYS if k in meta})
    versions.sort(key=lambda v: (v.get("seq") or 0, v.get("captured_at") or ""), reverse=True)
    latest = versions[0]["version"] == meta["version"]
    for k in ARTIFACT_KEYS:
        if latest or k not in record:
            record[k] = meta.get(k)
    record["versions"] = versions
    write_json(meta_path, record)
    return True


def push_vault(data_dir, key):
    spool = os.path.join(data_dir, "spool", key)
    target = read_json(os.path.join(spool, "vault.json"), {})
    url = target.get("vault")
    if not url:
        raise RuntimeError("spool has no vault.json")
    clone = os.path.join(data_dir, "vaults", key)
    with Lock(os.path.join(data_dir, "locks", f"{key}.lock")) as lock:
        if not lock.held:
            return {"skipped": "another session is pushing"}
        ensure_clone(url, clone)
        applied, dropped, titles, done = 0, 0, [], []
        for meta, folder in queued_versions(spool):
            if apply_version(clone, meta, folder):
                applied += 1
                titles.append(meta.get("title") or meta["id"])
            else:
                dropped += 1
            done.append(folder)
        # Commit whatever is in pages/ and meta/, including changes a failed earlier run left.
        if git("status", "--porcelain", "--", "pages", "meta", cwd=clone).strip():
            git("add", "--", "pages", "meta", cwd=clone)
            subject = f"archive: {titles[0]}" if applied == 1 else f"archive: {max(applied, 1)} versions"
            git("commit", "--quiet", "-m", subject, cwd=clone)
        for folder in done:  # only after the commit: the clone now holds their content
            shutil.rmtree(folder, ignore_errors=True)
        ahead = commits_to_push(clone)
        if ahead:
            push(clone)
        return {"applied": applied, "duplicates": dropped, "pushed": ahead}


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
        git("pull", "--rebase", "--quiet", cwd=clone)  # someone else pushed first
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
