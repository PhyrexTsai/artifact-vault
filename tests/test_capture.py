"""capture.py against fake payloads shaped like a real Artifact publish event.

Field names follow a recorded publish event; every value (paths, URLs, titles, ids) is fake.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(__file__)
SCRIPTS = os.path.join(HERE, "..", "plugin", "scripts")
sys.path.insert(0, SCRIPTS)
import capture  # noqa: E402

VAULT = "git@github.com:example-org/example-artifact.git"
ARTIFACT = "https://claude.ai/" + "artifact/"  # split: the leak scan blocks real artifact links
PAGE = '<title>Example Page</title>\n<meta name="vault:type" content="dev-plan">\n<p>hello</p>\n'


def make_project(config=True, files=None):
    root = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    if config:
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "artifact-vault.json"), "w") as fh:
            json.dump({"vault": VAULT}, fh)
    for path, text in (files or {"index.html": PAGE, "diagrams/d.html": "<svg></svg>\n"}).items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
    return root


def publish_event(root, files=None, **res):
    response = {
        "url": ARTIFACT + "ExAmPlEiD123", "path": os.path.join(root, "index.html"),
        "artifact_id": "00000000-0000-0000-0000-000000000000", "title": "Example Page",
        "updated": False, "icon": "doc", "audience": "owner", "seq": 1,
        "version": "1700000000-abcd", "contract": "0.0.0", "liveSubscription": "arming",
        "files_written": [{"path": "diagrams/d.html", "sha256": "0" * 64}],
    }
    response.update(res)
    return {
        "hook_event_name": "PostToolUse", "tool_name": "Artifact", "cwd": root,
        "session_id": "s", "tool_use_id": "t",
        "tool_input": {"file_path": os.path.join(root, "index.html"),
                       "files": {"diagrams/d.html": "diagrams/d.html"} if files is None else files,
                       "icon": "doc", "description": "An example page.", "__internalFlag": False},
        "tool_response": response,
    }


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.data = os.path.realpath(tempfile.mkdtemp())
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, "CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL": "author@example.com"}

    def run_capture(self, event):
        out = io.StringIO()
        with redirect_stdout(out):
            result = capture.capture(event, env=self.env)
        text = out.getvalue().strip()
        return result, (json.loads(text) if text else None)

    def spool_dirs(self):
        root = os.path.join(self.data, "spool")
        return [d for d, _, f in os.walk(root) if "meta.json" in f]

    def test_quickstart_is_skipped(self):
        root = make_project()
        event = {"tool_name": "Artifact", "cwd": root,
                 "tool_input": {"action": "quickstart", "intent": "other"}, "tool_response": {"quickstart": {}}}
        self.assertEqual(self.run_capture(event), (None, None))
        self.assertEqual(self.spool_dirs(), [])

    def test_read_and_asset_are_skipped(self):
        root = make_project()
        for extra in ({"action": "read"}, {"asset": True}):
            event = publish_event(root)
            event["tool_input"].update(extra)
            self.assertIsNone(self.run_capture(event)[0], extra)

    def test_publish_queues_page_files_and_meta(self):
        root = make_project()
        result, msg = self.run_capture(publish_event(root))
        self.assertEqual(result, "queued")
        self.assertIn("systemMessage", msg)
        self.assertIn("additionalContext", msg["hookSpecificOutput"])
        [spool] = self.spool_dirs()
        self.assertTrue(spool.endswith(os.path.join("ExAmPlEiD123", "1700000000-abcd")))
        self.assertTrue(os.path.isfile(os.path.join(spool, "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(spool, "diagrams", "d.html")))
        with open(os.path.join(spool, "meta.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["id"], "ExAmPlEiD123")
        self.assertEqual(meta["type"], "dev-plan")
        self.assertEqual(meta["audience"], "owner")
        self.assertEqual(meta["author"], "author@example.com")
        self.assertEqual(meta["files_written"], ["diagrams/d.html"])
        self.assertEqual(meta["seq"], 1)

    def test_vault_skip_is_skipped(self):
        root = make_project(files={"index.html": '<meta name="vault:skip">\n<p>private</p>\n'})
        self.assertEqual(self.run_capture(publish_event(root, files={})), (None, None))
        self.assertEqual(self.spool_dirs(), [])

    def test_vault_skip_far_down_the_page_is_honored(self):
        big = "<style>" + ("a{color:red}" * 2000) + "</style>\n<meta name=\"vault:skip\">\n"
        root = make_project(files={"index.html": big})
        self.assertEqual(self.run_capture(publish_event(root, files={}))[0], None)

    def test_unquoted_vault_skip_is_honored(self):
        root = make_project(files={"index.html": "<meta name=vault:skip>\n<p>x</p>\n"})
        self.assertEqual(self.run_capture(publish_event(root, files={}))[0], None)

    def test_vault_skip_inside_comment_or_script_is_ignored(self):
        page = '<!-- <meta name="vault:skip"> -->\n<script>var s = \'<meta name="vault:skip">\';</script>\n<p>x</p>\n'
        root = make_project(files={"index.html": page})
        self.assertEqual(self.run_capture(publish_event(root, files={}))[0], "queued")

    def test_no_config_is_skipped(self):
        root = make_project(config=False)
        self.assertEqual(self.run_capture(publish_event(root)), (None, None))

    def test_same_content_is_not_stored_twice(self):
        root = make_project()
        self.assertEqual(self.run_capture(publish_event(root))[0], "queued")
        result, msg = self.run_capture(publish_event(root, version="1700000001-ef01", seq=2))
        self.assertEqual(result, "unchanged")
        self.assertEqual(len(self.spool_dirs()), 1)
        with open(os.path.join(root, "diagrams", "d.html"), "w") as fh:
            fh.write("<svg>changed</svg>\n")
        self.assertEqual(self.run_capture(publish_event(root, version="1700000002-ef02", seq=3))[0], "queued")
        self.assertEqual(len(self.spool_dirs()), 2)

    def test_unsafe_published_paths_are_dropped(self):
        root = make_project()
        files = {"../escape.html": "diagrams/d.html", "/abs.html": "diagrams/d.html",
                 "a/../b.html": "diagrams/d.html", "index.html": "diagrams/d.html",
                 "ok/d.html": "diagrams/d.html"}
        result, msg = self.run_capture(publish_event(root, files=files))
        self.assertEqual(result, "queued")
        self.assertIn("略過 4", msg["systemMessage"])
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(json.load(fh)["files_written"], ["ok/d.html"])
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(spool), "escape.html")))

    def test_case_variants_of_reserved_and_duplicate_paths_are_dropped(self):
        root = make_project()
        files = {"Index.html": "diagrams/d.html", "META.JSON": "diagrams/d.html",
                 "Diagrams/D.html": "diagrams/d.html", "diagrams/d.html": "diagrams/d.html"}
        result, msg = self.run_capture(publish_event(root, files=files))
        self.assertEqual(result, "queued")
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "index.html")) as fh:
            self.assertEqual(fh.read(), PAGE)
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(len(json.load(fh)["files_written"]), 1)

    def test_file_folder_conflicts_drop_only_the_clashing_file(self):
        root = make_project()
        files = {"meta.json/child.css": "diagrams/d.html", "index.html/child.css": "diagrams/d.html",
                 "assets": "diagrams/d.html", "assets/child.css": "diagrams/d.html",
                 "deep/a.css": "diagrams/d.html", "Deep": "diagrams/d.html"}
        result, msg = self.run_capture(publish_event(root, files=files))
        self.assertEqual(result, "queued")
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            # sorted order decides which side of a clash is kept; the result is deterministic
            self.assertEqual(json.load(fh)["files_written"], ["Deep", "assets"])
        self.assertIn("略過 4", msg["systemMessage"])

    def test_credential_in_file_name_blocks_capture(self):
        key = "ghp_" + "B" * 36
        root = make_project()
        result, msg = self.run_capture(publish_event(root, files={f"{key}.txt": "diagrams/d.html"}))
        self.assertEqual(result, "secret")
        self.assertNotIn(key, json.dumps(msg))
        self.assertEqual(self.spool_dirs(), [])

    def test_credential_in_title_blocks_capture_without_echoing_it(self):
        key = "ghp_" + "A" * 36
        root = make_project()
        result, msg = self.run_capture(publish_event(root, title=f"notes {key}"))
        self.assertEqual(result, "secret")
        self.assertNotIn(key, json.dumps(msg))
        self.assertEqual(self.spool_dirs(), [])

    def test_missing_version_gets_unique_folder_per_content(self):
        root = make_project()
        self.assertEqual(self.run_capture(publish_event(root, version=None))[0], "queued")
        with open(os.path.join(root, "index.html"), "w") as fh:
            fh.write(PAGE + "<p>v2</p>\n")
        self.assertEqual(self.run_capture(publish_event(root, version=None))[0], "queued")
        self.assertEqual(len(self.spool_dirs()), 2)

    def test_republish_records_only_its_own_changes(self):
        root = make_project()
        self.assertEqual(self.run_capture(publish_event(root))[0], "queued")
        with open(os.path.join(root, "index.html"), "w") as fh:
            fh.write(PAGE + "<p>v2</p>\n")
        self.assertEqual(self.run_capture(publish_event(root, files={}, version="v2", seq=2))[0], "queued")
        self.run_capture(publish_event(root, files={"diagrams/d.html": None, "x.js": {"artifact": "a", "path": "x.js"}},
                                       version="v3", seq=3))
        metas = {}
        for d in self.spool_dirs():
            with open(os.path.join(d, "meta.json")) as fh:
                m = json.load(fh)
            metas[m["version"]] = m
        self.assertEqual(metas["1700000000-abcd"]["files_written"], ["diagrams/d.html"])
        self.assertEqual(metas["v2"]["files_written"], [])  # kept on the server, rebuilt by seq replay
        self.assertEqual(metas["v3"]["files_removed"], ["diagrams/d.html"])
        self.assertEqual(metas["v3"]["files_remote"], ["x.js"])
        self.assertEqual([metas[v]["seq"] for v in ("1700000000-abcd", "v2", "v3")], [1, 2, 3])

    def test_concurrent_publishes_each_keep_their_version(self):
        root = make_project(files={"index.html": PAGE, "diagrams/d.html": "x", "a.css": "a", "b.css": "b"})
        script = os.path.join(SCRIPTS, "capture.py")
        env = {**os.environ, **self.env}
        procs = []
        for name, ver, seq in (("a.css", "va", 2), ("b.css", "vb", 3)):
            ev = publish_event(root, files={name: name}, version=ver, seq=seq)
            pr = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env)
            pr.stdin.write(json.dumps(ev)); pr.stdin.close(); procs.append(pr)
        for pr in procs:
            pr.wait(timeout=30)
        found = sorted(os.path.basename(d) for d in self.spool_dirs())
        self.assertEqual(found, ["va", "vb"])

    def test_string_list_form(self):
        root = make_project()
        self.assertEqual(self.run_capture(publish_event(root, files=["diagrams/d.html"]))[0], "queued")
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(json.load(fh)["files_written"], ["diagrams/d.html"])

    def test_list_form_and_server_copies(self):
        root = make_project()
        files = {"diagrams/d.html": {"from": "diagrams/d.html"}, "copied.css": {"artifact": "x", "path": "a.css"},
                 "removed.js": None}
        result, _ = self.run_capture(publish_event(root, files=files))
        self.assertEqual(result, "queued")
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(json.load(fh)["files_written"], ["diagrams/d.html"])
        root2 = make_project()
        self.data = os.path.realpath(tempfile.mkdtemp()); self.env["CLAUDE_PLUGIN_DATA"] = self.data
        self.assertEqual(self.run_capture(publish_event(root2, files=[{"path": "diagrams/d.html"}]))[0], "queued")

    def test_credential_blocks_capture_without_echoing_it(self):
        key = "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        root = make_project(files={"index.html": f"<p>{key}</p>\n", "diagrams/d.html": "x"})
        result, msg = self.run_capture(publish_event(root))
        self.assertEqual(result, "secret")
        self.assertNotIn(key, json.dumps(msg))
        self.assertEqual(self.spool_dirs(), [])

    def test_type_attribute_order_and_default(self):
        root = make_project(files={"index.html": '<meta content="Research" name="vault:type">\n', "diagrams/d.html": "x"})
        self.run_capture(publish_event(root))
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(json.load(fh)["type"], "research")
        root2 = make_project(files={"index.html": "<p>no meta</p>\n", "diagrams/d.html": "x"})
        self.data = os.path.realpath(tempfile.mkdtemp()); self.env["CLAUDE_PLUGIN_DATA"] = self.data
        self.run_capture(publish_event(root2))
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(json.load(fh)["type"], "unsorted")

    def test_hostile_version_stays_inside_spool(self):
        root = make_project()
        self.run_capture(publish_event(root, version="../../etc"))
        [spool] = self.spool_dirs()
        self.assertTrue(os.path.realpath(spool).startswith(os.path.join(self.data, "spool")))

    def test_capabilities_recorded(self):
        root = make_project()
        event = publish_event(root)
        event["tool_input"]["capabilities"] = {"db": {}}
        self.run_capture(event)
        [spool] = self.spool_dirs()
        with open(os.path.join(spool, "meta.json")) as fh:
            self.assertEqual(json.load(fh)["capabilities"], ["db"])

    def test_cli_never_fails_the_session(self):
        for stdin in ["not json", json.dumps({"tool_input": {"file_path": "/nonexistent/x.html"},
                                              "tool_response": {"url": ARTIFACT + "x"},
                                              "cwd": make_project()})]:
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "capture.py")], input=stdin,
                               capture_output=True, text=True, env={**os.environ, **self.env})
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_end_to_end_prints_hook_json(self):
        root = make_project()
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "capture.py")],
                           input=json.dumps(publish_event(root)), capture_output=True, text=True,
                           env={**os.environ, **self.env})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemMessage", json.loads(r.stdout))


if __name__ == "__main__":
    unittest.main()
