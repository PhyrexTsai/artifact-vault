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
import push  # noqa: E402
import resolve  # noqa: E402

GUIDE = re.compile(r"^\s*<!--.*?-->\s*", re.S)


def archify_available(home=None):
    home = home or os.path.expanduser("~")
    skill = any(os.path.isfile(os.path.join(home, d, "skills", "archify", "SKILL.md")) for d in (".claude", ".agents"))
    return skill and shutil.which("node") is not None


def vault_clone(cwd, env=os.environ):
    """Clone or refresh the project's vault. Return (found, clone) or (None, None)."""
    found = resolve.resolve(cwd)
    if not found:
        return None, None
    data = env.get("CLAUDE_PLUGIN_DATA")
    if not data:
        raise SystemExit("artifact-vault: CLAUDE_PLUGIN_DATA is not set")
    clone = os.path.join(data, "vaults", found["key"])
    with push.Lock(os.path.join(data, "locks", f"{found['key']}.lock")) as lock:
        if lock.held:  # a running push holds it otherwise; reading the current clone is fine
            if os.path.isdir(os.path.join(clone, ".git")):
                push.recover(clone)
            push.ensure_clone(found["vault"], clone)
            push.configure(clone)
    if not os.path.isdir(clone):
        raise SystemExit("artifact-vault: the vault is not cloned yet; try again in a moment")
    return found, clone


def load_types(clone):
    cfg = push.read_json(os.path.join(clone, "vault.json"), {})
    types = {}
    for name, t in (cfg.get("types") or {}).items():
        template = os.path.join(clone, t.get("template", ""))
        root = os.path.realpath(clone)
        if not os.path.isfile(template) or os.path.commonpath([root, os.path.realpath(template)]) != root:
            continue
        types[name] = {"label": t.get("label", name), "template": template,
                       "diagram": [d for d in t.get("diagram", []) if d in ("archify", "mermaid")]}
    return cfg, types


def prepare(cwd, env=os.environ, home=None):
    found, clone = vault_clone(cwd, env)
    if not found:
        return None
    cfg, types = load_types(clone)
    tools = ["mermaid"] + (["archify"] if archify_available(home) else [])
    for t in types.values():
        t["use_diagram"] = next((d for d in t["diagram"] if d in tools), None)
    return {"vault": found["name"], "repo": found["repo"], "clone": clone, "types": types,
            "diagram_tools": tools, "css": os.path.join(clone, cfg.get("css", "")),
            "logo": os.path.join(clone, cfg.get("logo", ""))}


def render(kind, out, cwd, env=os.environ):
    info = prepare(cwd, env)
    if not info:
        raise SystemExit("NO_VAULT")
    t = info["types"].get(kind)
    if not t:
        raise SystemExit(f"artifact-vault: no template '{kind}'. Available: {', '.join(info['types']) or 'none'}")
    read = lambda p: open(p).read() if p and os.path.isfile(p) else ""
    page = GUIDE.sub("", read(t["template"]), count=1)
    page = page.replace("/*@@BASE_CSS@@*/", read(info["css"])).replace("<!--@@LOGO@@-->", read(info["logo"]).strip())
    if f'content="{kind}"' not in page:
        page = f'<meta name="vault:type" content="{kind}">\n' + page
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(page)
    return out


def main(argv):
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
