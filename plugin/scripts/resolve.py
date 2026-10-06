"""Decide which library repo (vault) a project archives into.

A project opts in with `<repo root>/.claude/artifact-vault.json`:

    { "vault": "git@github.com:example-org/example-artifact.git" }

`vault` is a git URL (ssh, https) or an absolute path to a local repo. Without the file,
or outside a git repo, the project has no vault and every hook and skill does nothing.

No network access and no writes: this runs inside a synchronous hook.

CLI: `python3 resolve.py [dir]` prints one JSON object, or NO_VAULT.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

CONFIG = os.path.join(".claude", "artifact-vault.json")
# scp-like [user@]host:path (any user, ssh aliases), ssh://, https://, file://
URL = re.compile(r"^((?:[\w.-]+@)?[\w.-]+:[^\s/]\S*|ssh://\S+|https://\S+|file://\S+)$")


def project_root(cwd):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                             capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def repo_name(root):
    """Name from the origin remote, so a worktree reports its repo, not its folder."""
    try:
        url = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=root,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    return vault_name(url) if url else os.path.basename(root)


def vault_name(url):
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip(".")
    return name or "vault"


def resolve(cwd):
    """Return a dict describing the vault for cwd, or None."""
    root = project_root(cwd)
    if not root:
        return None
    path = os.path.join(root, CONFIG)
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"artifact-vault: ignoring {CONFIG}: {e}", file=sys.stderr)
        return None
    url = cfg.get("vault") if isinstance(cfg, dict) else None
    if not isinstance(url, str) or not url.strip():
        print(f"artifact-vault: ignoring {CONFIG}: missing \"vault\"", file=sys.stderr)
        return None
    url = os.path.expanduser(url.strip())
    if os.path.isabs(url):
        url = os.path.normpath(url)
    elif not URL.match(url):
        print(f"artifact-vault: ignoring {CONFIG}: \"vault\" must be a git URL or an absolute path", file=sys.stderr)
        return None
    name = vault_name(url)
    # Two vaults can share a repo name (different owners), so the clone folder also
    # carries a short hash of the full URL.
    folder = f"{name}-{hashlib.sha256(url.encode()).hexdigest()[:10]}"
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return {
        "vault": url,
        "name": name,
        "clone": os.path.join(data, "vaults", folder) if data else None,
        "project": root,
        "repo": repo_name(root),
    }


def main(argv):
    found = resolve(argv[1] if len(argv) > 1 else os.getcwd())
    print(json.dumps(found) if found else "NO_VAULT")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
