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
        spool = os.path.join(self.data, "spool")
        if not os.path.isdir(spool):
            return {}  # nothing published or tagged on this machine yet
        return push.read_json(os.path.join(self.data, "state", os.listdir(spool)[0] + "-push.json"), {})


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
        return run("git", "log", "--all", "--full-history", "--format=%h", "--", path, cwd=self.bare).split()

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
        with mock.patch.object(push, "push_head", side_effect=RuntimeError("offline")):
            b.push()  # nothing pushed, nothing kept in b's clone: all stays queued
        self.purge()
        b.publish("Other", "v1", 1)
        result = b.push()
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("dropped_purged"), 1)
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])
        self.assertNotIn(b"secret three", all_objects(self.bare))
        self.assertIn("pages/Other/v1/index.html", self.remote_files())
        self.assertIn("pages/Keep/v2/index.html", self.remote_files())

    def test_a_purge_during_another_push_does_not_bring_the_page_back(self):
        b = Machine(self.bare)
        b.push()
        b.publish("Gone", "v3", 3, "secret three")
        real = push.git
        state = {"done": False}

        def purge_first(*args, **kw):  # a's purge lands between b's checks and b's push
            if args[:1] == ("push",) and not state["done"] and kw.get("cwd", "").startswith(b.data):
                state["done"] = True
                with mock.patch.object(push, "git", real):
                    self.purge()
            return real(*args, **kw)

        with mock.patch.object(push, "git", side_effect=purge_first):
            b.push()
        b.push()
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])
        self.assertIn("purged/Gone.json", self.remote_files())

    def test_a_clone_with_old_history_tags_after_a_purge(self):
        import vault_tag
        b = Machine(self.bare)
        with mock.patch.dict(os.environ, b.env):
            vault_tag.list_artifacts(b.root, b.env)  # b's clone has the old history
        self.purge()
        with mock.patch.dict(os.environ, b.env):
            self.assertIn("now research", vault_tag.set_type(b.root, "Keep", "research", "b@example.com", b.env))
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])
        self.assertTrue(any(n.startswith("overrides/Keep/") for n in self.remote_files()))

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

    def test_a_vault_that_only_held_the_page(self):
        base = os.path.realpath(tempfile.mkdtemp())
        bare = os.path.join(base, "solo-artifact.git")
        run("git", "init", "-q", "--bare", "-b", "main", bare)
        m = Machine(bare)
        m.publish("Gone", "v1", 1, "lonely secret")
        m.push()
        with mock.patch.dict(os.environ, m.env):
            vault_purge.run(m.root, "Gone", "Gone", None, m.env)
        run("git", "gc", "-q", "--prune=now", cwd=bare)
        self.assertEqual(run("git", "ls-tree", "-r", "--name-only", "main", cwd=bare).split(), ["purged/Gone.json"])
        self.assertNotIn(b"lonely secret", all_objects(bare))
        m.publish("Next", "v1", 1)
        self.assertTrue(m.push().get("ok"))
        self.assertIn("pages/Next/v1/index.html", run("git", "ls-tree", "-r", "--name-only", "main", cwd=bare))

    def test_after_a_rewrite_during_push_the_next_runs_keep_working(self):
        b = Machine(self.bare)
        b.publish("Gone", "v3", 3, "secret three")
        b.push()  # b's clone now has Gone and the old history
        b.publish("Other", "v1", 1)
        real = push.git
        state = {"done": False}

        def purge_first(*args, **kw):
            if args[:1] == ("push",) and not state["done"] and kw.get("cwd", "").startswith(b.data):
                state["done"] = True
                with mock.patch.object(push, "git", real):
                    self.purge()
            return real(*args, **kw)

        with mock.patch.object(push, "git", side_effect=purge_first):
            b.push()
        for _ in range(2):
            result = b.push()
            self.assertTrue(result.get("ok"), result)
        self.assertIn("pages/Other/v1/index.html", self.remote_files())
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])

    def test_a_page_only_queued_is_dropped_and_blocked(self):
        self.a.publish("Fresh", "v1", 1, "queued secret")
        with mock.patch.dict(os.environ, self.a.env):
            p = vault_purge.plan(self.a.root, "Fresh", self.a.env)
            self.assertEqual((p["files"], p["commits"], p["queued"]), (0, [], ["v1"]))
            vault_purge.run(self.a.root, "Fresh", "Fresh", None, self.a.env)
        self.a.push()
        self.assertIn("purged/Fresh.json", self.remote_files())
        self.assertNotIn(b"queued secret", all_objects(self.bare))

    def test_a_commit_holding_a_purged_page_is_never_pushed(self):
        self.purge()
        clone = os.path.join(self.a.data, "vaults", os.listdir(os.path.join(self.a.data, "vaults"))[0])
        os.makedirs(os.path.join(clone, "pages", "Gone", "v7"))
        with open(os.path.join(clone, "pages", "Gone", "v7", "index.html"), "w") as fh:
            fh.write("from an older plugin")
        run("git", "add", "-f", "pages", cwd=clone)
        run("git", "commit", "-qm", "old plugin", cwd=clone)
        with self.assertRaises(RuntimeError):
            push.push_head(clone)
        self.assertEqual(self.remote_history("pages/Gone"), [])

    def test_an_interrupted_rewrite_is_still_recognised(self):
        b = Machine(self.bare)
        b.publish("Gone", "v3", 3, "secret three")
        b.push()
        self.purge()
        clone = os.path.join(b.data, "vaults", os.listdir(os.path.join(b.data, "vaults"))[0])
        run("git", "-c", "core.hooksPath=/dev/null", "fetch", "-q", "origin", cwd=clone)  # killed right after this fetch
        b.publish("Other", "v1", 1)
        result = b.push()
        self.assertTrue(result.get("ok"), result)
        self.assertIn("pages/Other/v1/index.html", self.remote_files())
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Gone"), [])

    def test_a_clone_of_an_empty_vault_follows_the_first_push_elsewhere(self):
        import vault_tag
        base = os.path.realpath(tempfile.mkdtemp())
        bare = os.path.join(base, "fresh-artifact.git")
        run("git", "init", "-q", "--bare", "-b", "main", bare)
        b, c = Machine(bare), Machine(bare)
        with mock.patch.dict(os.environ, b.env):
            vault_tag.list_artifacts(b.root, b.env)  # b clones the vault while it is empty
        c.publish("First", "v1", 1)
        self.assertTrue(c.push().get("ok"))
        b.publish("Second", "v1", 1)
        self.assertTrue(b.push().get("ok"))
        names = run("git", "ls-tree", "-r", "--name-only", "main", cwd=bare)
        self.assertIn("pages/First/v1/index.html", names)
        self.assertIn("pages/Second/v1/index.html", names)

    def test_purge_before_the_vault_has_any_commit(self):
        base = os.path.realpath(tempfile.mkdtemp())
        bare = os.path.join(base, "unborn-artifact.git")
        run("git", "init", "-q", "--bare", "-b", "main", bare)
        m = Machine(bare)
        m.publish("Early", "v1", 1, "early secret")
        with mock.patch.dict(os.environ, m.env):
            p = vault_purge.plan(m.root, "Early", m.env)
            self.assertEqual((p["commits"], p["queued"]), ([], ["v1"]))
            vault_purge.run(m.root, "Early", "Early", None, m.env)
        self.assertEqual(run("git", "ls-tree", "-r", "--name-only", "main", cwd=bare).split(), ["purged/Early.json"])
        m.push()
        self.assertNotIn(b"early secret", all_objects(bare))

    def test_a_page_only_on_a_merged_side_branch_is_found_and_removed(self):
        seed = os.path.realpath(tempfile.mkdtemp())
        run("git", "clone", "-q", self.bare, seed)
        run("git", "checkout", "-q", "-b", "side", cwd=seed)
        os.makedirs(os.path.join(seed, "pages", "Side", "v1"))
        with open(os.path.join(seed, "pages", "Side", "v1", "index.html"), "w") as fh:
            fh.write("side secret")
        run("git", "add", "-A", cwd=seed)
        run("git", "commit", "-qm", "side", cwd=seed)
        run("git", "checkout", "-q", "main", cwd=seed)
        run("git", "merge", "-q", "-s", "ours", "--no-edit", "side", cwd=seed)  # the result drops the page
        run("git", "push", "-q", "origin", "main", cwd=seed)
        with mock.patch.dict(os.environ, self.a.env):
            self.assertTrue(vault_purge.plan(self.a.root, "Side", self.a.env)["commits"])
            vault_purge.run(self.a.root, "Side", "Side", None, self.a.env)
        run("git", "gc", "-q", "--prune=now", cwd=self.bare)
        self.assertEqual(self.remote_history("pages/Side"), [])
        self.assertNotIn(b"side secret", all_objects(self.bare))

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
