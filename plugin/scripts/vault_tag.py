"""Change the category (type) of an archived page.

  python3 vault_tag.py [--data <dir>] [--author <email>] list [dir]
  python3 vault_tag.py [--data <dir>] [--author <email>] set <id or artifact url> <type> [dir]

Versions are never edited. A category change is written to overrides/<id>.json in the
vault, committed through the same lock and checks as push (the commit may touch only that
file), and pushed. The library site applies overrides when it builds its index.
"""
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push  # noqa: E402
import vault_page  # noqa: E402

ID = re.compile(r"(?:/artifact/)?([A-Za-z0-9-]+)/?$")


class TagError(Exception):
    pass


def artifacts(clone):
    """{id: {title, type, versions, latest}} from pages/<id>/<version>/meta.json and overrides."""
    found = {}
    pages = os.path.join(clone, "pages")
    if not os.path.isdir(pages):
        return found
    for art in sorted(os.listdir(pages)):
        metas = [push.read_json(os.path.join(pages, art, v, "meta.json"), None)
                 for v in sorted(os.listdir(os.path.join(pages, art))) if os.path.isdir(os.path.join(pages, art, v))]
        metas = [m for m in metas if isinstance(m, dict)]
        if not metas:
            continue
        latest = max(metas, key=lambda m: (m.get("seq") or 0, m.get("captured_at") or ""))
        override = push.read_json(os.path.join(clone, "overrides", f"{art}.json"), {})
        found[art] = {"title": latest.get("title") or art, "type": override.get("type") or latest.get("type") or "unsorted",
                      "versions": len(metas), "latest": latest.get("version")}
    return found


def parse_id(text):
    m = ID.search(text.strip())
    if not m:
        raise TagError(f"not an artifact id or link: {text}")
    return m.group(1)


def set_type(cwd, ref, kind, author, env=os.environ):
    art = parse_id(ref)
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise TagError("this project has no vault")
        _, types = vault_page.load_types(clone)
        allowed = sorted(types) + ["unsorted"]
        if kind not in allowed:
            raise TagError(f"unknown type '{kind}'. Available: {', '.join(allowed)}")
        arts = artifacts(clone)
        if art not in arts:
            raise TagError(f"{art} is not in the library yet (it may still be waiting to be pushed)")
        if arts[art]["type"] == kind:
            return f"{art} is already {kind}"
        rel = f"overrides/{art}.json"
        path = os.path.join(clone, *rel.split("/"))
        push.inside_clone(clone, path)
        push.write_json(path, {"type": kind, "by": author or None,
                               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
        before = push.head(clone)
        push.git("add", "-f", "--", rel, cwd=clone)
        push.git("commit", "--quiet", "-m", f"tag: {arts[art]['title']} as {kind}", cwd=clone)
        problem = push.check_commit(clone, before, {rel: push.blob_id(path, push.git(
            "rev-parse", "--show-object-format", cwd=clone).strip() or "sha1")})
        if problem:
            push.undo_commit(clone, before)
            raise TagError(f"{problem}; nothing changed")
        data = env.get("CLAUDE_PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
        push.trusted_head(data, found["key"], clone, push.head(clone))
        # Register the vault for the background push, so a failed push below is retried
        # even on a machine that never published a page to this vault.
        route = os.path.join(data, "spool", found["key"], "vault.json")
        push.write_json(route, {"vault": found["vault"], "name": found["name"]})
        try:
            push.push(clone)
        except RuntimeError as e:
            return f"{art} is now {kind}; the commit is saved and will be pushed later ({e})"
        return f"{art} is now {kind}"


def list_artifacts(cwd, env=os.environ):
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise TagError("this project has no vault")
        return artifacts(clone)


def main(argv):
    args = argv[1:]
    author = os.environ.get("CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL", "")
    while args and args[0] in ("--data", "--author") and len(args) >= 2:
        if "${" not in args[1] and args[1]:
            if args[0] == "--data":
                os.environ["CLAUDE_PLUGIN_DATA"] = args[1]
            else:
                author = args[1]
                os.environ["CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL"] = author  # commit identity fallback
        args = args[2:]
    try:
        if args[:1] == ["list"]:
            rows = list_artifacts(args[1] if len(args) > 1 else os.getcwd())
            if not rows:
                print("The library has no pages yet.")
            for art, r in rows.items():
                print(f"{art}\t{r['type']}\t{r['versions']} version(s)\t{r['title']}")
            return 0
        if args[:1] == ["set"] and len(args) >= 3:
            print(set_type(args[3] if len(args) > 3 else os.getcwd(), args[1], args[2], author))
            return 0
    except (TagError, vault_page.VaultBusy) as e:
        print(f"artifact-vault: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
