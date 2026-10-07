"""Change the category (type) of an archived page.

  python3 vault_tag.py [--data <dir>] [--author <email>] list [dir]
  python3 vault_tag.py [--data <dir>] [--author <email>] set <id or artifact url> <type> [dir]

Versions are never edited. Each category change is a new file, overrides/<id>/<time>-<rand>.json,
committed through the same lock and checks as push (the commit may touch only that file) and
pushed. If it cannot be pushed, the commit is undone and the user is told to try again. No file is ever shared between changes, so two machines tagging the same page never
conflict; the newest change (by "at") wins. The library site applies it when it builds.
"""
import datetime
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push  # noqa: E402
import vault_page  # noqa: E402

ID = re.compile(r"[A-Za-z0-9-]+")
LINK = re.compile(r"/artifact/([A-Za-z0-9-]+)/?$")


class TagError(Exception):
    pass


def vault_json(clone, *parts):
    """Read JSON from inside the clone only: a symlink in the remote vault must not make the
    tag skill read a local file (its title would go into a pushed commit message)."""
    path = vault_page.vault_file(clone, "/".join(parts))
    return push.read_json(path, None) if path else None


def artifacts(clone):
    """{id: {title, type, versions, latest}} from pages/<id>/<version>/meta.json and overrides."""
    found = {}
    pages = os.path.join(clone, "pages")
    if not os.path.isdir(pages):
        return found
    for art in sorted(os.listdir(pages)):
        if not os.path.isdir(os.path.join(pages, art)):
            continue  # e.g. pages/README.md
        metas = [vault_json(clone, "pages", art, ver, "meta.json")
                 for ver in sorted(os.listdir(os.path.join(pages, art))) if os.path.isdir(os.path.join(pages, art, ver))]
        metas = [m for m in metas if isinstance(m, dict)]
        if not metas:
            continue
        latest = max(metas, key=lambda m: (m.get("seq") or 0, m.get("captured_at") or ""))
        override = latest_override(clone, art)
        found[art] = {"title": latest.get("title") or art, "type": override.get("type") or latest.get("type") or "unsorted",
                      "versions": len(metas), "latest": latest.get("version")}
    return found


def latest_override(clone, art):
    folder = os.path.join(clone, "overrides", art)
    changes = [vault_json(clone, "overrides", art, n) for n in sorted(os.listdir(folder))] \
        if os.path.isdir(folder) else []
    changes = [c for c in changes if isinstance(c, dict) and c.get("type")]
    return max(changes, key=lambda c: (c.get("at") or "", c.get("id") or "")) if changes else {}


def parse_id(text):
    """An artifact link, or a bare id matched in full (so a typo never selects another page)."""
    text = text.strip()
    if "/" in text:
        m = LINK.search(text)
        if m:
            return m.group(1)
    elif ID.fullmatch(text):
        return text
    raise TagError(f"not an artifact id or link: {text}")


def set_type(cwd, ref, kind, author, env=os.environ):
    art = parse_id(ref)
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise TagError("this project has no vault")
        _, types = vault_page.load_types(clone)
        allowed = sorted(types) + ["unsorted"]
        if kind not in allowed:
            raise TagError(f"unknown type '{kind}'. Available: {', '.join(allowed)}")
        for attempt in range(push.MAX_TRIES):
            if attempt:  # the remote moved (or was purged) since: start over from it
                push.mirror_remote(clone)
            arts = artifacts(clone)
            if art not in arts:
                raise TagError(f"{art} is not in the library yet (it may still be waiting to be pushed)")
            if arts[art]["type"] == kind:
                return f"{art} is already {kind}"
            now = datetime.datetime.now(datetime.timezone.utc)
            change = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
            rel = f"overrides/{art}/{change}.json"
            path = os.path.join(clone, *rel.split("/"))
            push.inside_clone(clone, path)
            push.write_json(path, {"type": kind, "by": author or None, "id": change,
                                   "at": now.isoformat(timespec="microseconds")})
            before = push.head(clone)
            push.git("add", "-f", "--", rel, cwd=clone)
            push.git("commit", "--quiet", "-m", f"tag: {arts[art]['title']} as {kind}", cwd=clone)
            problem = push.check_commit(clone, before, {rel: push.blob_id(path, push.git(
                "rev-parse", "--show-object-format", cwd=clone).strip() or "sha1")})
            if problem:
                push.undo_commit(clone, before)
                raise TagError(f"{problem}; nothing changed")
            try:
                push.push_head(clone)
                return f"{art} is now {kind}"
            except RuntimeError as e:
                push.undo_commit(clone, before)  # the clone never keeps unpushed work
                error = e
        raise TagError(f"could not push the change, so nothing changed; try again later ({error})")


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
