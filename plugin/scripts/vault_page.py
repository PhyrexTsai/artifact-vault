"""Helpers for the page skill.

  python3 vault_page.py [--data <plugin data dir>] prepare [dir]
      NO_VAULT when the project has not opted in. Otherwise makes sure the vault is cloned
      (and up to date), then prints JSON: vault name, page types from the vault's
      vault.json (label, template, diagram tools in order), and which diagram tools this
      machine has.

  python3 vault_page.py [--data <plugin data dir>] render <type> <out.html> [dir]
      Writes a starter page for <type>: the vault's template with its stylesheet and logo
      inlined and the guide comment removed. Claude then replaces the sample content.

Network access happens here (cloning or pulling the vault), not in the capture hook.
"""
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402
import push  # noqa: E402
import resolve  # noqa: E402

GUIDE = re.compile(r"^\s*<!--.*?-->\s*", re.S)


def archify_available(home=None):
    home = home or os.path.expanduser("~")
    skill = any(os.path.isfile(os.path.join(home, d, "skills", "archify", "SKILL.md")) for d in (".claude", ".agents"))
    return skill and shutil.which("node") is not None


class VaultBusy(Exception):
    pass


def locked_vault(cwd, env=os.environ, wait=15.0):
    """Context manager: (found, clone) with the vault lock held, the clone refreshed.
    Holding the lock while templates are read keeps a background push from swapping files
    in between. Yields (None, None) when the project has no vault."""
    import contextlib
    import time

    @contextlib.contextmanager
    def cm():
        found = resolve.resolve(cwd)
        if not found:
            yield None, None
            return
        data = env.get("CLAUDE_PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
        if not data:
            raise SystemExit("artifact-vault: CLAUDE_PLUGIN_DATA is not set")
        clone = os.path.join(data, "vaults", found["key"])
        deadline = time.monotonic() + wait
        while True:
            lock = push.Lock(os.path.join(data, "locks", f"{found['key']}.lock"))
            lock.__enter__()
            if lock.held:
                break
            lock.__exit__(None, None, None)
            if time.monotonic() > deadline:
                raise VaultBusy("the library is busy syncing; try again in a moment")
            time.sleep(0.2)
        try:
            push.refresh_clone(data, found["key"], found["vault"], clone)
            yield found, clone
        finally:
            lock.__exit__(None, None, None)

    return cm()


def vault_file(clone, rel):
    """Absolute path of a file inside the vault clone, or None. vault.json comes from the
    remote, so a path that is absolute, climbs out, or links out must not be read: it would
    embed a local file into a page that gets published."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    root = os.path.realpath(clone)
    path = os.path.realpath(os.path.join(clone, rel))
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        return None
    if os.path.relpath(path, root).split(os.sep)[0].lower() == ".git":
        return None  # local git metadata (config can hold credentials) is not vault content
    return path


def load_types(clone):
    cfg = push.read_json(os.path.join(clone, "vault.json"), {})
    types = {}
    for name, t in (cfg.get("types") or {}).items():
        template = vault_file(clone, t.get("template"))
        if not template:
            continue
        types[name] = {"label": t.get("label", name), "template": template,
                       "diagram": [d for d in t.get("diagram", []) if d in ("archify", "mermaid")]}
    return cfg, types


def describe(found, clone, home=None):
    cfg, types = load_types(clone)
    tools = ["mermaid"] + (["archify"] if archify_available(home) else [])
    for t in types.values():
        t["use_diagram"] = next((d for d in t["diagram"] if d in tools), None)
    return {"vault": found["name"], "repo": found["repo"], "clone": clone, "types": types,
            "diagram_tools": tools, "css": vault_file(clone, cfg.get("css")),
            "logo": vault_file(clone, cfg.get("logo"))}


def prepare(cwd, env=os.environ, home=None):
    with locked_vault(cwd, env) as (found, clone):
        return describe(found, clone, home) if found else None


def add_type(page, kind):
    """Insert the vault:type tag inside <head>, or after a doctype, never before it."""
    tag = f'<meta name="vault:type" content="{kind}">'
    head = re.search(r"<head\b[^>]*>", page, re.I)
    if head:
        return page[:head.end()] + "\n" + tag + page[head.end():]
    doctype = re.match(r"\s*<!doctype[^>]*>", page, re.I)
    if doctype:
        return page[:doctype.end()] + "\n" + tag + page[doctype.end():]
    return tag + "\n" + page


def render(kind, out, cwd, env=os.environ):
    """Write a starter page for a new file. Never overwrites: an existing page is edited in
    place, so its content and any skip marker survive."""
    if os.path.exists(out):
        raise SystemExit(f"artifact-vault: {out} already exists; edit it in place instead of rendering a new one")
    read = lambda p: open(p).read() if p and os.path.isfile(p) else ""
    with locked_vault(cwd, env) as (found, clone):  # read every file while the lock is held
        if not found:
            raise SystemExit("NO_VAULT")
        info = describe(found, clone)
        t = info["types"].get(kind)
        if not t:
            raise SystemExit(f"artifact-vault: no template '{kind}'. Available: {', '.join(info['types']) or 'none'}")
        template, css, logo = read(t["template"]), read(info["css"]), read(info["logo"]).strip()
    if not template.strip():
        raise SystemExit(f"artifact-vault: template '{kind}' is empty")
    page = GUIDE.sub("", template, count=1)
    page = page.replace("/*@@BASE_CSS@@*/", css).replace("<!--@@LOGO@@-->", logo)
    if capture.page_meta(page.encode()).get("vault:type", "").lower() != kind.lower():
        page = add_type(page, kind)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "x") as fh:  # x: fail rather than overwrite if it appeared meanwhile
        fh.write(page)
    return out


def main(argv):
    try:
        return run_cli(argv)
    except VaultBusy as e:
        print(f"artifact-vault: {e}", file=sys.stderr)
        return 1


def run_cli(argv):
    # The Bash tool does not export CLAUDE_PLUGIN_DATA, so the skill passes it: --data <dir>
    if len(argv) >= 3 and argv[1] == "--data":
        if argv[2] and "${" not in argv[2]:
            os.environ["CLAUDE_PLUGIN_DATA"] = argv[2]
        argv = [argv[0]] + argv[3:]
    if len(argv) >= 2 and argv[1] == "prepare":
        info = prepare(argv[2] if len(argv) > 2 else os.getcwd())
        print(json.dumps(info, ensure_ascii=False, indent=2) if info else "NO_VAULT")
        return 0
    if len(argv) >= 4 and argv[1] == "render":
        print(render(argv[2], argv[3], argv[4] if len(argv) > 4 else os.getcwd()))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
