"""vault_purge.py and push.py's handling of a purged, rewritten vault."""
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
import vault_purge  # noqa: E402
from test_vault_tag import ARTIFACT, IDENT, make_vault, run  # noqa: E402


class Machine:
    """One computer: its own plugin data folder and project checkout, same vault."""

    def __init__(self, bare):
        self.data = os.path.realpath(tempfile.mkdtemp())
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, **IDENT}
        self.root = os.path.realpath(tempfile.mkdtemp())
        run("git", "init", "-q", self.root)
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "artifact-vault.json"), "w") as fh:
            json.dump({"vault": bare}, fh)

    def publish(self, art, version, seq, text="x"):
        page = os.path.join(self.root, f"{art}-{version}.html")
        with open(page, "w") as fh:
            fh.write(f"<p>{text}</p>")
        event = {"tool_name": "Artifact", "cwd": self.root, "tool_input": {"file_path": page},
                 "tool_response": {"url": ARTIFACT + art, "title": f"{art} title", "version": version, "seq": seq}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)

    def push(self):
        with mock.patch.dict(os.environ, self.env):
            push.main(self.env)
        return push.read_json(os.path.join(self.data, "state", os.listdir(os.path.join(self.data, "spool"))[0] + "-push.json"), {})


def all_objects(repo):
    """Every object in the repository, as bytes (trees are binary)."""
    return subprocess.run(["git", "cat-file", "--batch-all-objects", "--batch"], cwd=repo, check=True,
                          capture_output=True).stdout


class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.bare = make_vault()
        self.a = Machine(self.bare)
        self.patch = mock.patch.dict(os.environ, self.a.env)
        self.patch.start()
        self.a.publish("Gone", "v1", 1, "secret one")
        self.a.publish("Keep", "v1", 1)
        self.a.push()
        self.a.publish("Gone", "v2", 2, "secret two")
        self.a.push()

    def tearDown(self):
        self.patch.stop()

    def remote_history(self, path):
        return run("git", "log", "--all", "--format=%h", "--", path, cwd=self.bare).split()

    def remote_files(self):
        return run("git", "ls-tree", "-r", "--name-only", "main", cwd=self.bare).split()

    def purge(self, m=None, confirm="Gone"):
        m = m or self.a
        with mock.patch.dict(os.environ, m.env):
            return vault_purge.run(m.root, ARTIFACT + "Gone", confirm, "me@example.com", m.env)

    def test_plan_lists_without_changing_anything(self):
        before = run("git", "rev-parse", "main", cwd=self.bare)
        p = vault_purge.plan(self.a.root, "Gone", self.a.env)
        self.assertEqual(p["title"], "Gone title")
        self.assertEqual(len(p["commits"]), 2)
        self.assertEqual(p["files"], 4)
        self.assertEqual(run("git", "rev-parse", "main", cwd=self.bare), before)

    def test_wrong_confirmation_changes_nothing(self):
        before = run("git", "rev-parse", "main", cwd=self.bare)
        with self.assertRaises(vault_purge.PurgeError):
            self.purge(confirm="gone")
        self.assertEqual(run("git", "rev-parse", "main", cwd=self.bare), before)

    def test_page_leaves_the_branch_and_all_history(self):
        self.purge()
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])
        files = self.remote_files()
        self.assertIn("pages/Keep/v1/index.html", files)
        self.assertIn("purged/Gone.json", files)
        self.assertEqual(run("git", "log", "-1", "--format=%s", "main", cwd=self.bare).strip(), "purge: Gone")
        objects = all_objects(self.bare)
        self.assertNotIn(b"secret one", objects)
        self.assertNotIn(b"secret two", objects)
        clone = os.path.join(self.a.data, "vaults", os.listdir(os.path.join(self.a.data, "vaults"))[0])
        self.assertNotIn(b"secret two", all_objects(clone))

    def test_another_machine_neither_brings_it_back_nor_loses_its_own_work(self):
        b = Machine(self.bare)
        b.publish("Keep", "v2", 2)
        b.publish("Gone", "v3", 3, "secret three")  # queued on b, purged meanwhile
        b.push()  # b's clone now has the old history
        b.publish("Other", "v1", 1)
        with mock.patch.object(push, "push", side_effect=RuntimeError("offline")):
            b.push()  # committed in b's clone, not pushed
        self.purge()
        result = b.push()
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("history_rewritten"))
        self.assertEqual(result.get("respooled"), 1)
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])
        self.assertIn("pages/Other/v1/index.html", self.remote_files())
        self.assertIn("pages/Keep/v2/index.html", self.remote_files())

    def test_queued_versions_of_a_purged_page_are_dropped(self):
        b = Machine(self.bare)
        b.publish("Keep", "v2", 2)
        b.push()
        self.purge()
        b.publish("Gone", "v9", 9, "again")
        result = b.push()
        self.assertEqual(result.get("dropped_purged"), 1)
        self.assertEqual(self.remote_history("pages/Gone"), [])

    def test_backfill_refuses_a_purged_page(self):
        self.purge()
        folder = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(folder, "index.html"), "w") as fh:
            fh.write("<p>x</p>")
        self.assertEqual(vault_backfill.add(self.a.root, ARTIFACT + "Gone", "v5", folder, env=self.a.env)[0], "refused")

    def test_a_concurrent_push_makes_the_purge_stop(self):
        b = Machine(self.bare)
        real = push.git

        def racing(*args, **kw):  # someone pushes right before the forced push
            if args[:1] == ("push",) and any(str(a).startswith("--force-with-lease") for a in args):
                b.publish("Late", "v1", 1)
                b.push()
            return real(*args, **kw)

        with mock.patch.object(push, "git", side_effect=racing):
            with self.assertRaises(vault_purge.PurgeError):
                self.purge()
        self.assertIn("pages/Late/v1/index.html", self.remote_files())  # nothing of theirs lost
        self.purge()  # running it again works
        self.assertEqual(self.remote_history("pages/Gone"), [])
        self.assertIn("pages/Late/v1/index.html", self.remote_files())

    def test_cli_plan_and_run(self):
        script = os.path.join(HERE, "..", "plugin", "scripts", "vault_purge.py")
        env = {k: v for k, v in {**os.environ, **IDENT}.items() if k != "CLAUDE_PLUGIN_DATA"}
        out = subprocess.run([sys.executable, script, "--data", self.a.data, "plan", "Gone", "--in", self.a.root],
                             capture_output=True, text=True, env=env)
        self.assertEqual(json.loads(out.stdout)["files"], 4, out.stderr)
        out = subprocess.run([sys.executable, script, "--data", self.a.data, "run", "Gone", "--in", self.a.root],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 2)  # no --confirm: usage, nothing changed
        out = subprocess.run([sys.executable, script, "--data", self.a.data, "run", "Gone", "--confirm", "Gone",
                              "--in", self.a.root], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.remote_history("pages/Gone"), [])


if __name__ == "__main__":
    unittest.main()
