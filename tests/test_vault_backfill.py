"""vault_backfill.py against a local bare vault."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "plugin", "scripts"))
import capture  # noqa: E402
import push  # noqa: E402
import vault_backfill  # noqa: E402
from test_vault_tag import ARTIFACT, IDENT, make_vault, run  # noqa: E402


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.data = os.path.realpath(tempfile.mkdtemp())
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, "CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL": "me@example.com", **IDENT}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.bare = make_vault()
        self.root = os.path.realpath(tempfile.mkdtemp())
        run("git", "init", "-q", self.root)
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "artifact-vault.json"), "w") as fh:
            json.dump({"vault": self.bare}, fh)

    def tearDown(self):
        self.patch.stop()

    def read_back(self, files):
        """A folder as the Artifact tool's read with paths/out_dir leaves it."""
        folder = os.path.realpath(tempfile.mkdtemp())
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(folder, rel)), exist_ok=True)
            with open(os.path.join(folder, rel), "w") as fh:
                fh.write(text)
        return folder

    def add(self, art, version, files, **kw):
        return vault_backfill.add(self.root, ARTIFACT + art, version, self.read_back(files), env=self.env, **kw)

    def remote(self, path):
        return run("git", "show", f"main:{path}", cwd=self.bare)

    def publish(self, art, version, seq):
        page = os.path.join(self.root, f"{version}.html")
        with open(page, "w") as fh:
            fh.write(f"<p>{version}</p>")
        event = {"tool_name": "Artifact", "cwd": self.root, "tool_input": {"file_path": page},
                 "tool_response": {"url": ARTIFACT + art, "title": "T", "version": version, "seq": seq}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)

    def test_page_and_supporting_files_reach_the_vault(self):
        status, msg = self.add("Old1", "1791299089-6c62", {
            "index.html": '<title>Old</title><meta name="vault:type" content="plan"><iframe src="diagrams/a.html"></iframe>',
            "diagrams/a.html": "<svg></svg>"}, title="Old plan", capabilities=["db"])
        self.assertEqual(status, "queued", msg)
        push.main(self.env)
        meta = json.loads(self.remote("pages/Old1/1791299089-6c62/meta.json"))
        self.assertEqual(meta["title"], "Old plan")
        self.assertEqual(meta["type"], "plan")
        self.assertEqual(meta["source"], "backfill")
        self.assertEqual(meta["author"], "me@example.com")
        self.assertEqual(meta["files_written"], ["diagrams/a.html"])
        self.assertEqual(meta["capabilities"], ["db"])
        self.assertEqual(self.remote("pages/Old1/1791299089-6c62/diagrams/a.html"), "<svg></svg>")

    def test_a_version_already_archived_or_queued_is_skipped(self):
        self.assertEqual(self.add("Old1", "v1", {"index.html": "<p>a</p>"})[0], "queued")
        self.assertEqual(self.add("Old1", "v1", {"index.html": "<p>a</p>"})[0], "skipped")  # still in the spool
        push.main(self.env)
        self.assertEqual(self.add("Old1", "v1", {"index.html": "<p>a</p>"})[0], "skipped")  # now in the vault

    def test_backfilled_version_sorts_after_stored_ones(self):
        self.publish("Mixed", "p1", 1)
        self.publish("Mixed", "p2", 2)
        push.main(self.env)
        self.add("Mixed", "live", {"index.html": "<p>live</p>"})
        push.main(self.env)
        self.assertEqual(json.loads(self.remote("pages/Mixed/live/meta.json"))["seq"], 3)

    def test_first_backfills_count_up_from_one_even_within_a_second(self):
        self.add("NoSeq", "a", {"index.html": "<title>A</title>"}, title="old title")
        self.add("NoSeq", "b", {"index.html": "<title>B</title>"}, title="new title")
        push.main(self.env)
        self.assertEqual(json.loads(self.remote("pages/NoSeq/a/meta.json"))["seq"], 1)
        self.assertEqual(json.loads(self.remote("pages/NoSeq/b/meta.json"))["seq"], 2)
        import vault_tag
        self.assertEqual(vault_tag.list_artifacts(self.root, self.env)["NoSeq"]["title"], "new title")

    def test_a_later_publish_sorts_after_a_backfill(self):
        self.add("Later", "b1", {"index.html": "<p>b</p>"})
        self.publish("Later", "p5", 5)  # its real publish count
        push.main(self.env)
        import vault_tag
        self.assertEqual(vault_tag.list_artifacts(self.root, self.env)["Later"]["latest"], "p5")

    def test_meta_linked_from_outside_the_clone_is_not_read(self):
        self.add("Sym1", "v1", {"index.html": "<p>1</p>"})
        push.main(self.env)
        private = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(private, "meta.json"), "w") as fh:
            json.dump({"id": "Sym1", "version": "evil", "seq": 1, "files_written": ["private-name.txt"]}, fh)
        seed = os.path.realpath(tempfile.mkdtemp())
        run("git", "clone", "-q", self.bare, seed)
        os.makedirs(os.path.join(seed, "pages", "Sym1", "evil"))
        os.symlink(os.path.join(private, "meta.json"), os.path.join(seed, "pages", "Sym1", "evil", "meta.json"))
        run("git", "add", "-A", cwd=seed)
        run("git", "commit", "-qm", "link", cwd=seed)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
        self.add("Sym1", "v2", {"index.html": "<p>2</p>"})
        push.main(self.env)
        meta = json.loads(self.remote("pages/Sym1/v2/meta.json"))
        self.assertNotIn("private-name.txt", json.dumps(meta))

    def test_supporting_files_capture_cannot_keep_are_reported(self):
        status, msg = self.add("Part1", "v1", {"index.html": "<p>x</p>", "meta.json": "{}", "ok.css": "c"})
        self.assertEqual(status, "partial", msg)
        self.assertIn("1 supporting", msg)

    def test_skip_token_and_credentials_are_honoured(self):
        self.assertEqual(self.add("S1", "v1", {"index.html": "<!-- vault:skip --><p>x</p>"})[0], "skipped")
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        status, msg = self.add("S2", "v1", {"index.html": "<p>x</p>", "data.json": f'{{"k":"{key}"}}'})
        self.assertEqual(status, "refused")
        self.assertNotIn(key, msg)
        push.main(self.env)
        names = run("git", "ls-tree", "-r", "--name-only", "main", cwd=self.bare)
        self.assertNotIn("pages/S1", names)
        self.assertNotIn("pages/S2", names)

    def test_files_the_live_page_dropped_are_recorded_as_removed(self):
        self.add("Drop1", "v1", {"index.html": "<p>1</p>", "old.js": "1", "keep.css": "k"})
        push.main(self.env)
        self.add("Drop1", "v2", {"index.html": "<p>2</p>", "keep.css": "k"})
        push.main(self.env)
        meta = json.loads(self.remote("pages/Drop1/v2/meta.json"))
        self.assertEqual(meta["files_removed"], ["old.js"])
        self.assertEqual(meta["files_written"], ["keep.css"])

    def test_a_relative_files_folder_is_read_from_where_it_was_given(self):
        folder = self.read_back({"index.html": "<p>rel</p>", "a.css": "x"})
        here = os.getcwd()
        os.chdir(os.path.dirname(folder))
        try:
            status, msg = vault_backfill.add(self.root, ARTIFACT + "Rel1", "v1", os.path.basename(folder), env=self.env)
        finally:
            os.chdir(here)
        self.assertEqual(status, "queued", msg)
        push.main(self.env)
        self.assertEqual(self.remote("pages/Rel1/v1/a.css"), "x")

    def test_check_reports_missing_and_archived(self):
        self.add("Have1", "v1", {"index.html": "<p>a</p>"})
        rows = {r[0]: r for r in vault_backfill.check(self.root, [ARTIFACT + "Have1", "Miss1"], self.env)}
        self.assertEqual(rows["Have1"][1:], ("archived", 1, ["v1"]))
        self.assertEqual(rows["Miss1"][1:], ("missing", 0, []))

    def test_bad_input_is_rejected(self):
        with self.assertRaises(vault_backfill.BackfillError):
            vault_backfill.add(self.root, ARTIFACT + "X1", "v1", self.read_back({"other.html": "x"}), env=self.env)
        with self.assertRaises(vault_backfill.BackfillError):
            vault_backfill.add(self.root, "X1", "v1", self.read_back({"index.html": "x"}), env=self.env)
        with self.assertRaises(vault_backfill.BackfillError):
            vault_backfill.add(self.root, ARTIFACT + "X1", "...", self.read_back({"index.html": "x"}), env=self.env)

    def test_symlinks_in_the_read_folder_are_not_followed(self):
        outside = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(outside, "secret.txt"), "w") as fh:
            fh.write("local only")
        folder = self.read_back({"index.html": "<p>x</p>"})
        os.symlink(os.path.join(outside, "secret.txt"), os.path.join(folder, "leak.txt"))
        os.symlink(outside, os.path.join(folder, "dir"))
        self.assertEqual(vault_backfill.add(self.root, ARTIFACT + "L1", "v1", folder, env=self.env)[0], "queued")
        push.main(self.env)
        names = run("git", "ls-tree", "-r", "--name-only", "main", cwd=self.bare)
        self.assertNotIn("leak.txt", names)
        self.assertNotIn("secret.txt", names)

    def test_cli_add_and_check(self):
        folder = self.read_back({"index.html": "<p>a</p>"})
        out = subprocess.run([sys.executable, os.path.join(HERE, "..", "plugin", "scripts", "vault_backfill.py"),
                              "--data", self.data, "--author", "${user_config.author_email}", "add", "--url", ARTIFACT + "C1",
                              "--version", "v9", "--files", folder, "--in", self.root],
                             capture_output=True, text=True, env={**os.environ, **self.env})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("queued\t"))
        out = subprocess.run([sys.executable, os.path.join(HERE, "..", "plugin", "scripts", "vault_backfill.py"),
                              "--data", self.data, "check", "C1", "--in", self.root],
                             capture_output=True, text=True, env={**os.environ, **self.env})
        self.assertEqual(out.stdout.strip(), "C1\tarchived\t1\tv9")
        subprocess.run([sys.executable, os.path.join(HERE, "..", "plugin", "scripts", "push.py"), "--data", self.data],
                       check=True, env={k: v for k, v in {**os.environ, **IDENT}.items() if k != "CLAUDE_PLUGIN_DATA"})
        self.assertIn("pages/C1/v9/index.html", run("git", "ls-tree", "-r", "--name-only", "main", cwd=self.bare))


if __name__ == "__main__":
    unittest.main()
