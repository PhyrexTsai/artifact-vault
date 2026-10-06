"""Fail if this public repo leaks private details, now or anywhere in its history.

Checks:
  1. No file version reachable from HEAD, no tracked file in the working tree, no path,
     and no commit message contains a pattern. A secret added in one commit and deleted
     in the next still fails, because the old version stays in git history.
  2. Every commit author and committer email is a GitHub noreply address.

Patterns come from two places:
  - Built-in generic patterns (local paths, artifact links).
  - LEAK_PATTERNS: newline-separated private strings (project names, emails), kept in a
    CI secret so that the list itself never appears in the repo. Required when CI=true.

Reports never contain matched text: CI logs of a public repo are public. A path that
itself matches a pattern is printed as <redacted path>.
"""
import os
import re
import subprocess
import sys

# Split so that this file does not match its own patterns.
BUILTIN = ["/Use" + "rs/", "/ho" + "me/", "claude.ai/" + "artifact/", "claude.ai/code/" + "artifact/"]
NOREPLY = re.compile(
    r"^((\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com|noreply@github\.com)$", re.I
)


def git(*args, cwd, inp=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, input=inp).stdout


def patterns_from_env():
    private = [p.strip() for p in os.environ.get("LEAK_PATTERNS", "").splitlines() if p.strip()]
    if os.environ.get("CI") == "true" and not private:
        raise SystemExit("leak-scan: LEAK_PATTERNS is empty. Set the repository secret before CI can pass.")
    return [p.lower() for p in BUILTIN + private]


def first_match(text, pats):
    low = text.lower()
    return next((k for k, p in enumerate(pats) if p in low), None)


def shown(path, pats):
    return "<redacted path>" if first_match(path, pats) is not None else path


def scan_text(label, data, pats, hits, where=""):
    if b"\0" in data[:8192]:
        return  # binary
    for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        k = first_match(line, pats)
        if k is not None:
            hits.append(f"{label}:{n}{where} matches pattern #{k}")


def scan_paths_and_blobs(root, pats):
    """Every blob reachable from HEAD (all history) plus tracked working-tree files."""
    hits, seen = [], set()
    objects = git("rev-list", "--objects", "HEAD", cwd=root).decode("utf-8", "replace").splitlines()
    named = [tuple(line.split(" ", 1)) for line in objects if " " in line]
    # Paths are checked per commit tree, separately from blob dedupe: rev-list names each
    # blob once, so a rename that keeps the content would otherwise hide a private name.
    paths = set()
    for commit in git("rev-list", "HEAD", cwd=root).decode().split():
        paths.update(git("ls-tree", "-r", "-z", "--name-only", commit, cwd=root).decode("utf-8", "replace").split("\0"))
    paths.update(git("ls-files", "-z", cwd=root).decode("utf-8", "replace").split("\0"))
    for path in sorted(p for p in paths if p):
        k = first_match(path, pats)
        if k is not None:
            hits.append(f"<redacted path> (path matches pattern #{k})")
    if named:
        out = git("cat-file", "--batch-check=%(objectname) %(objecttype)", cwd=root,
                  inp="\n".join(s for s, _ in named).encode()).decode().split()
        kinds = dict(zip(out[0::2], out[1::2]))
        for sha, path in named:
            if kinds.get(sha) != "blob" or sha in seen:
                continue
            seen.add(sha)
            scan_text(shown(path, pats), git("cat-file", "blob", sha, cwd=root), pats, hits, f" (history {sha[:12]})")
    for path in git("ls-files", "-z", cwd=root).decode().split("\0"):
        if not path:
            continue
        try:
            with open(os.path.join(root, path), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        sha = git("hash-object", "--", path, cwd=root).decode().strip()
        if sha in seen:
            continue
        seen.add(sha)
        scan_text(shown(path, pats), data, pats, hits)
    return hits


def scan_commits(root, pats):
    hits = []
    log = git("log", "--format=%H%x1f%ae%x1f%ce%x1f%B%x1e", "HEAD", cwd=root).decode("utf-8", "replace")
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, author, committer, msg = rec.split("\x1f", 3)
        for role, email in (("author", author), ("committer", committer)):
            if not NOREPLY.match(email):
                hits.append(f"commit {sha[:12]} {role} email is not a GitHub noreply address")
        k = first_match(msg, pats)
        if k is not None:
            hits.append(f"commit {sha[:12]} message matches pattern #{k}")
    return hits


def main(root="."):
    pats = patterns_from_env()
    hits = scan_paths_and_blobs(root, pats) + scan_commits(root, pats)
    for h in hits:
        print(f"leak-scan: {h}")
    if hits:
        print(f"leak-scan: {len(hits)} problem(s). Pattern numbers: 0-{len(BUILTIN) - 1} are built-in, the rest come from LEAK_PATTERNS.")
        return 1
    print(f"leak-scan: clean ({len(pats)} patterns).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
