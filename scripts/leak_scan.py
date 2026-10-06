"""Fail if tracked files or commit history leak private details into this public repo.

Checks:
  1. Tracked text files and commit messages must not contain any pattern.
  2. Every commit author and committer email must be a GitHub noreply address.

Patterns come from two places:
  - Built-in generic patterns (local paths, artifact links).
  - LEAK_PATTERNS: newline-separated private strings (project names, emails), kept in a
    CI secret so that the list itself never appears in the repo. Required when CI=true.

Matches are reported by file, line, and pattern number only. The matched text is never
printed, because CI logs of a public repo are public.
"""
import os
import re
import subprocess
import sys

# Split so that this file does not match its own patterns.
BUILTIN = ["/Use" + "rs/", "/ho" + "me/", "claude.ai/" + "artifact/", "claude.ai/code/" + "artifact/"]
NOREPLY = re.compile(r"(^\d+\+[^@]+@users\.noreply\.github\.com$)|(^noreply@github\.com$)", re.I)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def patterns_from_env():
    private = [p.strip() for p in os.environ.get("LEAK_PATTERNS", "").splitlines() if p.strip()]
    if os.environ.get("CI") == "true" and not private:
        raise SystemExit("leak-scan: LEAK_PATTERNS is empty. Set the repository secret before CI can pass.")
    return [p.lower() for p in BUILTIN + private]


def scan_files(root, pats):
    hits = []
    for path in git("ls-files", "-z", cwd=root).decode().split("\0"):
        if not path:
            continue
        try:
            with open(os.path.join(root, path), "rb") as fh:
                text = fh.read()
        except OSError:
            continue
        if b"\0" in text[:8192]:
            continue  # binary
        lines = text.decode("utf-8", "replace").lower().splitlines()
        for n, line in enumerate(lines, 1):
            for k, p in enumerate(pats):
                if p in line:
                    hits.append(f"{path}:{n} matches pattern #{k}")
    return hits


def scan_history(root, pats):
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
        low = msg.lower()
        for k, p in enumerate(pats):
            if p in low:
                hits.append(f"commit {sha[:12]} message matches pattern #{k}")
    return hits


def main(root="."):
    pats = patterns_from_env()
    hits = scan_files(root, pats) + scan_history(root, pats)
    for h in hits:
        print(f"leak-scan: {h}")
    if hits:
        print(f"leak-scan: {len(hits)} problem(s). Pattern numbers: 0-{len(BUILTIN) - 1} are built-in, the rest come from LEAK_PATTERNS.")
        return 1
    print(f"leak-scan: clean ({len(pats)} patterns).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
