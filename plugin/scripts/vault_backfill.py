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

A version already in the library or the spool is skipped. A backfilled version sorts after
the versions the library already has: its seq is one more than the largest stored seq, or
none (ordered by capture time) when stored versions have no seq.
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
    """{version: meta} from the vault clone and the spool (queued, not yet pushed)."""
    found = {}
    for base in (os.path.join(clone, "pages", art), os.path.join(spool, art)):
        if not os.path.isdir(base):
            continue
        for ver in sorted(os.listdir(base)):
            meta = push.read_json(os.path.join(base, ver, "meta.json"), None) \
                if os.path.isfile(os.path.join(base, ver, "meta.json")) and not os.path.islink(os.path.join(base, ver)) else None
            if isinstance(meta, dict) and meta.get("id") == art and meta.get("version") == ver:
                found[ver] = meta
    return found


def next_seq(versions):
    seqs = [m["seq"] for m in versions.values() if isinstance(m.get("seq"), int) and not isinstance(m.get("seq"), bool)]
    return max(seqs) + 1 if seqs else None


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
    are added, removed ones dropped, server-side copies added)."""
    files = set()
    for m in sorted(versions.values(), key=lambda m: (m.get("seq") or 0, m.get("captured_at") or "")):
        files |= {p for p in m.get("files_written") or [] if isinstance(p, str)}
        files -= {p for p in m.get("files_removed") or [] if isinstance(p, str)}
        files |= {r.get("path") for r in m.get("files_remote") or [] if isinstance(r, dict) and isinstance(r.get("path"), str)}
    return files


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
        meta_path = os.path.join(spool, art, safe, "meta.json")
        meta = push.read_json(meta_path, {})
        meta["source"] = "backfill"  # still under the lock: no push can take it before this
        push.write_json(meta_path, meta)
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
            return 0 if status in ("queued", "skipped") else 1
    except (BackfillError, vault_tag.TagError, vault_page.VaultBusy) as e:
        print(f"artifact-vault: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
