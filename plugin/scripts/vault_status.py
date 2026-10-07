"""Status report for the setup skill.

  python3 vault_status.py [--data <plugin data dir>] [--author <email>] [dir]

Prints a short report: tools, the plugin version, this project's vault, the author email,
pages waiting to be pushed, and the last push result for every vault on this machine.
Reads only; never clones, pushes, or writes.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import push  # noqa: E402
import resolve  # noqa: E402


def tool_version(cmd):
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None


def plugin_version():
    manifest = os.path.join(HERE, "..", ".claude-plugin", "plugin.json")
    return push.read_json(manifest, {}).get("version", "unknown")


def vaults(data):
    """[(key, name, pending, last)] for every vault on this machine. Unpushed work is only ever
    in the spool: the clone never keeps a commit it could not push."""
    keys = set()
    for sub in ("spool", "vaults"):
        d = os.path.join(data, sub)
        if os.path.isdir(d):
            keys |= {k for k in os.listdir(d) if os.path.isdir(os.path.join(d, k))}
    rows = []
    for key in sorted(keys):
        spool = os.path.join(data, "spool", key)
        name = push.read_json(os.path.join(spool, "vault.json"), {}).get("name") or key
        pending = len(push.queued_versions(spool)) if os.path.isdir(spool) else 0
        last = push.read_json(os.path.join(data, "state", f"{key}-push.json"), None)
        rows.append((key, name, pending, last))
    return rows


def report(cwd, data, author):
    lines, problems = [], []
    py = f"Python {sys.version.split()[0]}"
    gitv = tool_version(["git", "--version"])
    lines.append(f"Tools: {py}; {gitv or 'git not found'}")
    if not gitv:
        problems.append("git is not installed: pages cannot be pushed.")
    lines.append(f"Plugin: artifact-vault {plugin_version()}")

    found = resolve.resolve(cwd)
    if found:
        lines.append(f"This project: archives into {found['name']} ({found['vault']}), repo {found['repo']}")
    else:
        lines.append("This project: no vault (add .claude/artifact-vault.json to opt in)")

    if author and "${" not in author:
        lines.append(f"Author email: {author}")
    else:
        lines.append("Author email: not set")
        problems.append("Set the author email: /plugin, select artifact-vault, Configure options.")

    if not data or "${" in data:
        problems.append("The plugin data folder is unknown, so the queue cannot be checked.")
    else:
        rows = vaults(data)
        if not rows:
            lines.append("Queue: nothing archived on this machine yet")
        for key, name, pending, last in rows:
            if last is None:
                state = "never pushed"
            elif last.get("ok"):
                state = f"last push OK at {last.get('at')}"
            else:
                state = f"last push FAILED at {last.get('at')}: {last.get('error')}"
                problems.append(f"{name}: {last.get('error')}")
            waiting = f"{pending} waiting"
            lines.append(f"Vault {name}: {waiting}; {state}")
            if pending and last and last.get("ok"):
                problems.append(f"{name}: pages are waiting; they are pushed at the end of the next turn.")
    return lines, problems


def main(argv):
    args, data, author = argv[1:], os.environ.get("CLAUDE_PLUGIN_DATA", ""), os.environ.get("CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL", "")
    while args and args[0] in ("--data", "--author") and len(args) >= 2:
        if args[0] == "--data" and "${" not in args[1]:
            data = args[1] or data
        if args[0] == "--author" and "${" not in args[1]:
            author = args[1] or author
        args = args[2:]
    lines, problems = report(args[0] if args else os.getcwd(), data, author)
    print("\n".join(lines))
    print("\nTo fix:" if problems else "\nAll good.")
    for p in problems:
        print(f"- {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
