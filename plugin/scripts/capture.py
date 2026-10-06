"""PostToolUse hook for the Artifact tool: queue a published page for the project's vault.

Runs synchronously, so it only copies local files into a spool folder and writes a
version meta file. It never clones, commits, or uses the network; push.sh does that later.

Skips quietly when: the call is not a publish (quickstart, read, list, ...), it uploads an
asset, it has no file_path, the project has no vault, or the page carries
<meta name="vault:skip">. Skips with a message when the page looks like it contains a
credential. Every other publish is kept; duplicates are dropped at push time, in seq order.

Never fails the session: unexpected errors go to stderr and the hook exits 0.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve  # noqa: E402

TYPE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
ARTIFACT_ID = re.compile(r"/artifact/([A-Za-z0-9-]+)/?$")

# Credential shapes. Only the kind is ever reported, never the match.
SECRETS = [
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(rb"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic API key", re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(rb"\bsk-(proj-)?[A-Za-z0-9_-]{32,}")),
    ("Slack token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key", re.compile(rb"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
]


class _Meta(HTMLParser):
    """Collect <meta name=... content=...> from real elements; comments and scripts are ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            name = a.get("name", "").strip().lower()
            if name.startswith("vault:") and name not in self.found:
                self.found[name] = a.get("content", "").strip()

    handle_startendtag = handle_starttag


def page_meta(html_bytes):
    """vault:* meta tags anywhere in the document."""
    parser = _Meta()
    try:
        parser.feed(html_bytes.decode("utf-8", "replace"))
        parser.close()
    except Exception:  # malformed HTML: keep what was found so far
        pass
    return parser.found


def out(system=None, context=None):
    msg = {}
    if system:
        msg["systemMessage"] = system
    if context:
        msg["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": context}
    if msg:
        print(json.dumps(msg, ensure_ascii=False))


def safe_published(path):
    """A published path must stay inside the version folder."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return None
    if any(p in ("", ".", "..") for p in path.split("/")):
        return None
    return path


def supporting_files(inp, cwd):
    """Yield (published_path, change, source_path) for tool_input.files.

    change is "set" (local source), "remove" (null), "remote" (copied from another
    artifact on the server, no local source), or "bad" (unsafe path).
    """
    files = inp.get("files")
    base = cwd
    if isinstance(inp.get("root"), str):
        base = os.path.join(cwd, os.path.expanduser(inp["root"]))
    items = []
    if isinstance(files, list):
        for f in files:
            path = f if isinstance(f, str) else f.get("path") if isinstance(f, dict) else None
            items.append((path, path))
    elif isinstance(files, dict):
        items = list(files.items())
    for pub, src in items:
        clean = safe_published(pub)
        if clean is None:
            yield None, "bad", None
        elif src is None:
            yield clean, "remove", None
        elif isinstance(src, dict) and not isinstance(src.get("from"), str):
            yield clean, "remote", None
        else:
            src = src["from"] if isinstance(src, dict) else src
            if not isinstance(src, str):
                yield clean, "bad", None
            else:
                yield clean, "set", os.path.normpath(src if os.path.isabs(src) else os.path.join(base, src))


def drop_conflicts(extras):
    """Keep files that can all be written into one folder next to index.html and meta.json.

    Compared case-insensitively (macOS, Windows): a reserved name, a duplicate, or a path
    that is both a file and a folder is dropped instead of failing the whole version.
    """
    files, dirs, kept = {"index.html", "meta.json"}, set(), []
    for pub, data in extras:
        low = pub.lower()
        parts = low.split("/")
        ancestors = {"/".join(parts[:i]) for i in range(1, len(parts))}
        if low in files or low in dirs or ancestors & files:
            continue
        files.add(low)
        dirs |= ancestors
        kept.append((pub, data))
    return kept, len(extras) - len(kept)


def find_secret(blobs):
    for data in blobs:
        for kind, rx in SECRETS:
            if rx.search(data):
                return kind
    return None


def digest(main, extras):
    h = hashlib.sha256(main)
    for pub, data in sorted(extras):
        h.update(b"\0" + pub.encode() + b"\0" + data)
    return h.hexdigest()


def write_version(spool, main, extras, meta):
    tmp = f"{spool}.tmp{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with open(os.path.join(tmp, "index.html"), "wb") as fh:
        fh.write(main)
    for pub, data in extras:
        dest = os.path.join(tmp, *pub.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
    with open(os.path.join(tmp, "meta.json"), "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    shutil.rmtree(spool, ignore_errors=True)
    os.makedirs(os.path.dirname(spool), exist_ok=True)
    os.replace(tmp, spool)


def capture(event, env=os.environ):
    """Return what happened: None (not ours), "secret", or "queued"."""
    inp = event.get("tool_input") or {}
    res = event.get("tool_response") or {}
    if inp.get("action", "publish") != "publish" or inp.get("asset") or not inp.get("file_path"):
        return None
    if not isinstance(res, dict) or not isinstance(res.get("url"), str):
        return None
    m = ARTIFACT_ID.search(res["url"])
    if not m:
        return None
    art_id = m.group(1)
    cwd = event.get("cwd") or os.getcwd()
    found = resolve.resolve(cwd)
    if not found:
        return None
    data_dir = env.get("CLAUDE_PLUGIN_DATA")
    if not data_dir:
        print("artifact-vault: CLAUDE_PLUGIN_DATA is not set; nothing queued", file=sys.stderr)
        return None

    main_path = inp["file_path"]
    with open(main_path if os.path.isabs(main_path) else os.path.join(cwd, main_path), "rb") as fh:
        main = fh.read()
    metas = page_meta(main)
    if "vault:skip" in metas:
        return None

    # Store only what this publish changed. A republish keeps the files it does not list,
    # so the full file set of a version is rebuilt later by replaying versions in seq order
    # (written files replace, removed files drop). That keeps capture free of shared state.
    written, removed, remote, problems = [], [], [], []
    for pub, change, src in supporting_files(inp, cwd):
        if change == "bad":
            problems.append("unsafe published path")
        elif change == "remove":
            removed.append(pub)
        elif change == "remote":
            remote.append(pub)
        else:
            try:
                with open(src, "rb") as fh:
                    written.append((pub, fh.read()))
            except OSError:
                problems.append(f"missing source for {pub}")
    written, clashes = drop_conflicts(sorted(written))  # sorted: clashes resolve the same way every time
    problems += ["path conflict"] * clashes

    title = str(res.get("title") or art_id)
    names = "\n".join([pub for pub, _ in written] + removed + remote).encode("utf-8")
    kind = find_secret([main, title.encode("utf-8"), names] + [d for _, d in written])
    if kind:  # the title may itself hold the credential, so name the page by id only
        out(system=f"artifact-vault：artifact {art_id} 看起來含有 {kind}，沒有存進書庫。移除後重新發佈即可。",
            context=f"artifact-vault did not queue this page: it appears to contain a {kind}.")
        return "secret"

    # Every publish is kept. Skipping duplicates needs the versions in seq order, which only
    # push time has, so the digest is recorded here and compared there.
    dig = digest(main, written + [(f"-{r}", b"") for r in sorted(removed)] + [(f"~{r}", b"") for r in sorted(remote)])

    now = datetime.datetime.now(datetime.timezone.utc)
    version = re.sub(r"[^A-Za-z0-9._-]", "-", str(res.get("version") or "")).strip(".") \
        or f"{now.strftime('%Y%m%dT%H%M%SZ')}-{dig[:8]}"  # no version: time plus content hash
    page_type = metas.get("vault:type", "").lower()
    caps = inp.get("capabilities")
    meta = {
        "id": art_id,
        "url": res["url"],
        "title": title,
        "version": version,
        "seq": res.get("seq"),
        "audience": res.get("audience"),
        "author": env.get("CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL") or None,
        "repo": found["repo"],
        "type": page_type if TYPE_NAME.match(page_type) else "unsorted",
        "files_written": sorted(pub for pub, _ in written),
        "files_removed": sorted(removed),
        "files_remote": sorted(remote),
        "capabilities": sorted(caps.keys()) if isinstance(caps, dict) else [],
        "agent_type": event.get("agent_type"),
        "captured_at": now.isoformat(timespec="seconds"),
        "digest": dig,
    }
    write_version(os.path.join(data_dir, "spool", found["key"], art_id, version), main, written, meta)

    note = f"（略過 {len(problems)} 個子檔案）" if problems else ""
    out(system=f"artifact-vault：已排入書庫 {found['name']}：「{title}」{note}",
        context=f"artifact-vault queued \"{title}\" ({art_id}, version {version}, type {meta['type']}) "
                f"for library {found['name']}. It is pushed in the background.")
    return "queued"


def main():
    try:
        capture(json.load(sys.stdin))
    except Exception as e:  # never break the session
        print(f"artifact-vault: capture failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
