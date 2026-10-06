"""PostToolUse hook for the Artifact tool: queue a published page for the project's vault.

Runs synchronously, so it only copies local files into a spool folder and writes a
version meta file. It never clones, commits, or uses the network; push.sh does that later.

Skips quietly when: the call is not a publish (quickstart, read, list, ...), it uploads an
asset, it has no file_path, the project has no vault, or the page carries
<meta name="vault:skip">. Skips with a message when the content did not change or when it
looks like it contains a credential.

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
    if path == "index.html" or path.startswith("meta.json"):
        return None  # reserved names inside the version folder
    return path


def supporting_files(inp, cwd):
    """Yield (published_path, source_path_or_None, problem_or_None) for tool_input.files."""
    files = inp.get("files")
    base = cwd
    if isinstance(inp.get("root"), str):
        base = os.path.join(cwd, os.path.expanduser(inp["root"]))
    items = []
    if isinstance(files, list):
        items = [(f.get("path"), f.get("path")) if isinstance(f, dict) else (None, None) for f in files]
    elif isinstance(files, dict):
        for pub, src in files.items():
            if isinstance(src, dict):
                src = src.get("from")  # an {artifact, path} copy has no local source
            items.append((pub, src))
    for pub, src in items:
        clean = safe_published(pub)
        if clean is None:
            yield None, None, "unsafe published path"
        elif not isinstance(src, str):
            yield clean, None, None  # removal or server-side copy: nothing local to keep
        else:
            yield clean, os.path.normpath(src if os.path.isabs(src) else os.path.join(base, src)), None


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
    tmp = spool + ".tmp"
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
    """Return what happened: None (not ours), "secret", "unchanged", or "queued"."""
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

    extras, problems = [], []
    for pub, src, problem in supporting_files(inp, cwd):
        if problem:
            problems.append(problem)
        elif src:
            try:
                with open(src, "rb") as fh:
                    extras.append((pub, fh.read()))
            except OSError:
                problems.append(f"missing source for {pub}")

    title = res.get("title") or art_id
    kind = find_secret([main] + [d for _, d in extras])
    if kind:
        out(system=f"artifact-vault：「{title}」看起來含有 {kind}，沒有存進書庫。移除後重新發佈即可。",
            context=f"artifact-vault did not queue this page: it appears to contain a {kind}.")
        return "secret"

    vault_key = found["key"]
    state_path = os.path.join(data_dir, "state", vault_key, f"{art_id}.json")
    dig = digest(main, extras)
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    if state.get("digest") == dig:
        out(system=f"artifact-vault：「{title}」內容沒有變，書庫不重存。")
        return "unchanged"

    now = datetime.datetime.now(datetime.timezone.utc)
    version = re.sub(r"[^A-Za-z0-9._-]", "-", str(res.get("version") or "")).strip(".") or now.strftime("%Y%m%dT%H%M%SZ")
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
        "files": sorted(pub for pub, _ in extras),
        "capabilities": sorted(caps.keys()) if isinstance(caps, dict) else [],
        "agent_type": event.get("agent_type"),
        "captured_at": now.isoformat(timespec="seconds"),
        "digest": dig,
    }
    write_version(os.path.join(data_dir, "spool", vault_key, art_id, version), main, extras, meta)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump({"digest": dig, "version": version}, fh)

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
