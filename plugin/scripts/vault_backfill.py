"""Add pages published before the project used a vault (or outside it) to the library.

  python3 vault_backfill.py [--data <dir>] [--author <email>] check <id or link>... [--in <dir>]
      One line per page: id, "missing" or "archived", how many versions the library holds,
      and their version names. Pending versions in the spool count as archived.

  python3 vault_backfill.py [--data <dir>] [--author <email>] add --url <link> --version <ver>
          --files <folder> [--title <title>] [--capabilities a,b] [--in <dir>]
      Queues one version whose files Claude already read back from claude.ai into <folder>
      (index.html plus supporting files at their published paths). The version goes through
      the same checks as a publish (vault:skip, credentials, unsafe paths) and into the same
      spool, so the background push stores it like any other version.

A version already in the library or the spool is skipped, and so is one older than a stored
version (by the publish time in version names, when they have it). A backfilled version sorts
after the versions the library already has: its seq is one more than the largest stored seq
(1 when there is none). It is marked "snapshot": it holds the whole page, so replaying files
starts over at it, and an older version pushed later from another machine cannot add back a
file the page no longer has.
"""
import contextlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402
import push  # noqa: E402
import vault_page  # noqa: E402
import vault_tag  # noqa: E402


class BackfillError(Exception):
    pass


def stored_versions(clone, spool, art):
    """{version: meta} from the vault clone and the spool (queued, not yet pushed). Vault files
    are read only from inside the clone: a symlink in the remote vault must not make this read
    local files (their file names would go into the pushed meta.json)."""
    found = {}
    if os.path.isdir(os.path.join(clone, "pages", art)):
        for ver in sorted(os.listdir(os.path.join(clone, "pages", art))):
            meta = vault_tag.vault_json(clone, "pages", art, ver, "meta.json")
            if isinstance(meta, dict) and meta.get("id") == art and meta.get("version") == ver:
                found[ver] = meta
    queued = os.path.join(spool, art)
    if os.path.isdir(queued) and not os.path.islink(queued):
        for ver in sorted(os.listdir(queued)):
            path = os.path.join(queued, ver, "meta.json")
            if os.path.islink(os.path.join(queued, ver)) or os.path.islink(path):
                continue
            meta = push.read_json(path, None)
            if isinstance(meta, dict) and meta.get("id") == art and meta.get("version") == ver:
                found[ver] = meta
    return found


def next_seq(versions):
    """One more than the largest stored seq, or 1. A publish reports the artifact's publish
    count as seq, so this never exceeds the backfilled version's real seq: later publishes
    still sort after it, and two backfills in the same second keep their order."""
    seqs = [m["seq"] for m in versions.values() if isinstance(m.get("seq"), int) and not isinstance(m.get("seq"), bool)]
    return max(seqs, default=0) + 1


def check(cwd, refs, env=os.environ):
    arts = [vault_tag.parse_id(r) for r in refs]
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise BackfillError("this project has no vault")
        spool = os.path.join(env["CLAUDE_PLUGIN_DATA"], "spool", found["key"])
        rows = []
        for art in arts:
            versions = stored_versions(clone, spool, art)
            rows.append((art, "archived" if versions else "missing", len(versions), sorted(versions)))
        return rows


def files_as_of(versions):
    """The page's supporting files after replaying the stored versions in order (written files
    are added, removed ones dropped, server-side copies added). A snapshot version (a
    backfill) holds the whole page, so it starts the set over."""
    files = set()
    for m in sorted(versions.values(), key=lambda m: (m.get("seq") or 0, m.get("captured_at") or "")):
        if m.get("snapshot"):
            files = set()
        files |= {p for p in m.get("files_written") or [] if isinstance(p, str)}
        files -= {p for p in m.get("files_removed") or [] if isinstance(p, str)}
        files |= {r.get("path") for r in m.get("files_remote") or [] if isinstance(r, dict) and isinstance(r.get("path"), str)}
    return files


PUBLISHED_AT = re.compile(r"^(\d{10})-")  # observed version names start with the publish time (unix seconds)


def published_at(version):
    """Publish time read from a version name, or None. The format is observed, not documented,
    so it is only used to refuse an older version, never to order versions."""
    m = PUBLISHED_AT.match(version or "")
    return int(m.group(1)) if m else None


def page_files(folder):
    """{published path: absolute path} for every file under folder except index.html."""
    out = {}
    for d, dirs, names in os.walk(folder):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
        for n in names:
            path = os.path.join(d, n)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if rel != "index.html" and not os.path.islink(path):
                out[rel] = path
    return out


def add(cwd, url, version, folder, title=None, capabilities=(), env=os.environ):
    art = vault_tag.parse_id(url)
    folder = os.path.abspath(folder)  # capture resolves relative paths against the project, not here
    if not vault_tag.LINK.search(url):
        raise BackfillError("--url must be the artifact link (ending in /artifact/<id>)")
    main = os.path.join(folder, "index.html")
    if not os.path.isfile(main) or os.path.islink(main):
        raise BackfillError(f"{folder} has no index.html; read the page with the Artifact tool first")
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise BackfillError("this project has no vault")
        spool = os.path.join(env["CLAUDE_PLUGIN_DATA"], "spool", found["key"])
        versions = stored_versions(clone, spool, art)
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", version).strip(".")  # the same name capture stores
        if not safe:
            raise BackfillError("--version is empty")
        if safe in versions:
            return "skipped", f"{art} {safe} is already in the library"
        mine = published_at(safe)
        newer = [v for v in versions if mine is not None and (published_at(v) or 0) > mine]
        if newer:  # a publish captured meanwhile is newer: this seq would wrongly sort after it
            return "skipped", f"{art} {safe} is older than {max(newer)}, which the library already has"
        # A read-back is the whole page, but a version stores only its changes: files the
        # stored versions still have and the page no longer has are recorded as removed.
        files = page_files(folder)
        files.update({gone: None for gone in files_as_of(versions) - set(files)})
        event = {
            "tool_name": "Artifact", "cwd": cwd,
            "tool_input": {"file_path": main, "files": files,
                           **({"capabilities": {c: {} for c in capabilities}} if capabilities else {})},
            "tool_response": {"url": url, "title": title or art, "version": version, "seq": next_seq(versions)},
        }
        hook_out = io.StringIO()
        with contextlib.redirect_stdout(hook_out):  # the hook prints JSON for Claude Code; not wanted here
            result = capture.capture(event, env=env)
        if result == "secret":
            return "refused", f"{art} looks like it contains a credential; nothing was queued"
        if result != "queued":
            return "skipped", f"{art} was not queued (the page contains vault:skip)"
        try:  # capture reports supporting files it could not keep (unsafe path, name clash)
            note = json.loads(hook_out.getvalue() or "{}").get("systemMessage", "")
        except ValueError:
            note = ""
        partial = re.search(r"略過 (\d+) 個子檔案", note)
        meta_path = os.path.join(spool, art, safe, "meta.json")
        meta = push.read_json(meta_path, {})
        meta["source"] = "backfill"  # still under the lock: no push can take it before this
        meta["snapshot"] = True  # holds every file of the page: replay starts over here
        push.write_json(meta_path, meta)
        if partial:
            return "partial", (f"{art} {safe} queued as \"{meta.get('title')}\" without {partial.group(1)} supporting "
                               "file(s) whose path could not be stored; the rest is pushed at the end of this turn")
        return "queued", f"{art} {safe} queued as \"{meta.get('title')}\" ({meta.get('type')}); it is pushed at the end of this turn"


def main(argv):
    args = argv[1:]
    env = dict(os.environ)
    while args and args[0] in ("--data", "--author") and len(args) >= 2:
        if args[1] and "${" not in args[1]:  # an unset plugin option arrives unexpanded
            env["CLAUDE_PLUGIN_DATA" if args[0] == "--data" else "CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL"] = args[1]
        args = args[2:]
    os.environ.update(env)  # push.configure() reads the author from the process environment
    opts, rest = {}, []
    it = iter(args[1:])
    for a in it:
        if a in ("--url", "--version", "--files", "--title", "--capabilities", "--in"):
            opts[a[2:]] = next(it, "")
        else:
            rest.append(a)
    cwd = opts.get("in") or os.getcwd()
    try:
        if args[:1] == ["check"] and rest:
            for art, status, n, names in check(cwd, rest, env):
                print(f"{art}\t{status}\t{n}\t{','.join(names)}")
            return 0
        if args[:1] == ["add"] and all(opts.get(k) for k in ("url", "version", "files")):
            caps = [c for c in opts.get("capabilities", "").split(",") if c]
            status, msg = add(cwd, opts["url"], opts["version"], opts["files"], opts.get("title"), caps, env)
            print(f"{status}\t{msg}")
            return 0 if status in ("queued", "partial", "skipped") else 1
    except (BackfillError, vault_tag.TagError, vault_page.VaultBusy) as e:
        print(f"artifact-vault: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
