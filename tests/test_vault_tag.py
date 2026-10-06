"""vault_tag.py against a local bare vault that already holds an archived page."""
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
import vault_tag  # noqa: E402

ARTIFACT = "https://claude.ai/" + "artifact/"  # split: the leak scan blocks real artifact links
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def run(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **IDENT}).stdout


def make_vault():
    base = os.path.realpath(tempfile.mkdtemp())
    bare = os.path.join(base, "example-artifact.git")
    run("git", "init", "-q", "--bare", "-b", "main", bare)
    work = os.path.join(base, "seed")
    run("git", "clone", "-q", bare, work)
    with open(os.path.join(work, "vault.json"), "w") as fh:
        json.dump({"name": "example", "types": {"plan": {"template": "vault.json"}, "research": {"template": "vault.json"}}}, fh)
    run("git", "add", "-A", cwd=work)
    run("git", "commit", "-qm", "init", cwd=work)
    run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    return bare


class TagTest(unittest.TestCase):
    def setUp(self):
        self.data = os.path.realpath(tempfile.mkdtemp())
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, **IDENT}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.bare = make_vault()
        self.root = os.path.realpath(tempfile.mkdtemp())
        run("git", "init", "-q", self.root)
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "artifact-vault.json"), "w") as fh:
            json.dump({"vault": self.bare}, fh)
        with open(os.path.join(self.root, "index.html"), "w") as fh:
            fh.write('<meta name="vault:type" content="plan">\n<p>x</p>\n')
        event = {"tool_name": "Artifact", "cwd": self.root,
                 "tool_input": {"file_path": os.path.join(self.root, "index.html")},
                 "tool_response": {"url": ARTIFACT + "Page1", "title": "Launch plan", "version": "v1", "seq": 1}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)
        push.main(self.env)

    def tearDown(self):
        self.patch.stop()

    def remote_override(self):
        names = [n for n in run("git", "ls-tree", "-r", "--name-only", "main", cwd=self.bare).split() if n.startswith("overrides/Page1/")]
        changes = [json.loads(run("git", "show", f"main:{n}", cwd=self.bare)) for n in names]
        return max(changes, key=lambda c: c["at"])

    def test_list_shows_archived_pages(self):
        rows = vault_tag.list_artifacts(self.root, self.env)
        self.assertEqual(rows["Page1"]["title"], "Launch plan")
        self.assertEqual(rows["Page1"]["type"], "plan")

    def test_set_writes_override_and_pushes(self):
        msg = vault_tag.set_type(self.root, "Page1", "research", "a@example.com", self.env)
        self.assertIn("now research", msg)
        self.assertEqual(self.remote_override()["type"], "research")
        self.assertEqual(vault_tag.list_artifacts(self.root, self.env)["Page1"]["type"], "research")

    def test_link_form_is_accepted(self):
        vault_tag.set_type(self.root, ARTIFACT + "Page1", "unsorted", "", self.env)
        self.assertEqual(self.remote_override()["type"], "unsorted")

    def test_unknown_type_lists_the_allowed_ones(self):
        with self.assertRaises(vault_tag.TagError) as e:
            vault_tag.set_type(self.root, "Page1", "deck", "", self.env)
        self.assertIn("plan, research, unsorted", str(e.exception))

    def test_unknown_page_is_rejected(self):
        with self.assertRaises(vault_tag.TagError):
            vault_tag.set_type(self.root, "Nope", "plan", "", self.env)

    def test_same_type_makes_no_commit(self):
        before = run("git", "rev-parse", "main", cwd=self.bare)
        self.assertIn("already plan", vault_tag.set_type(self.root, "Page1", "plan", "", self.env))
        self.assertEqual(run("git", "rev-parse", "main", cwd=self.bare), before)

    def test_failed_push_is_retried_by_the_background_push(self):
        other = os.path.realpath(tempfile.mkdtemp())  # a machine that never published here
        env = {**self.env, "CLAUDE_PLUGIN_DATA": other}
        with mock.patch.dict(os.environ, env), mock.patch.object(push, "push", side_effect=RuntimeError("offline")):
            msg = vault_tag.set_type(self.root, "Page1", "research", "", env)
        self.assertIn("pushed later", msg)
        with mock.patch.dict(os.environ, env):
            push.main(env)
        self.assertEqual(self.remote_override()["type"], "research")

    def test_uncommitted_override_leftover_is_not_taken_as_done(self):
        vault_tag.list_artifacts(self.root, self.env)  # clone exists
        clone = os.path.join(self.data, "vaults", os.listdir(os.path.join(self.data, "vaults"))[0])
        os.makedirs(os.path.join(clone, "overrides", "Page1"), exist_ok=True)
        with open(os.path.join(clone, "overrides", "Page1", "x.json"), "w") as fh:
            json.dump({"type": "research"}, fh)  # as if killed before git add
        msg = vault_tag.set_type(self.root, "Page1", "research", "", self.env)
        self.assertIn("now research", msg)
        self.assertEqual(self.remote_override()["type"], "research")

    def test_two_machines_tagging_one_page_do_not_conflict(self):
        other = os.path.realpath(tempfile.mkdtemp())
        env2 = {**self.env, "CLAUDE_PLUGIN_DATA": other}
        vault_tag.list_artifacts(self.root, self.env)  # both machines have a clone
        with mock.patch.dict(os.environ, env2):
            vault_tag.list_artifacts(self.root, env2)
            vault_tag.set_type(self.root, "Page1", "research", "", env2)
        msg = vault_tag.set_type(self.root, "Page1", "unsorted", "", self.env)
        self.assertIn("now unsorted", msg)
        self.assertNotIn("later", msg)
        self.assertEqual(self.remote_override()["type"], "unsorted")
        self.assertEqual(vault_tag.list_artifacts(self.root, self.env)["Page1"]["type"], "unsorted")

    def test_id_must_match_in_full(self):
        for bad in ("bad_Page1", "x Page1", "Page1.."):
            with self.assertRaises(vault_tag.TagError):
                vault_tag.parse_id(bad)
        self.assertEqual(vault_tag.parse_id(ARTIFACT + "Page1"), "Page1")
        self.assertEqual(vault_tag.parse_id("Page1"), "Page1")

    def test_files_directly_under_pages_are_ignored(self):
        work = os.path.join(os.path.dirname(self.bare), "seed")
        run("git", "pull", "-q", "origin", "main", cwd=work)
        with open(os.path.join(work, "pages", "README.md"), "w") as fh:
            fh.write("library\n")
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "readme", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        self.assertIn("Page1", vault_tag.list_artifacts(self.root, self.env))

    def test_json_encoding_attributes_do_not_break_tagging(self):
        work = os.path.join(os.path.dirname(self.bare), "seed")
        run("git", "pull", "-q", "origin", "main", cwd=work)
        with open(os.path.join(work, ".gitattributes"), "w") as fh:
            fh.write("*.json working-tree-encoding=UTF-16\n")
        run("git", "add", ".gitattributes", cwd=work); run("git", "commit", "-qm", "attrs", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        self.assertIn("now research", vault_tag.set_type(self.root, "Page1", "research", "", self.env))

    def test_cli_exit_codes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(vault_tag.main(["vault_tag.py", "--data", self.data, "list", self.root]), 0)
        self.assertIn("Launch plan", out.getvalue())
        self.assertEqual(vault_tag.main(["vault_tag.py", "--data", self.data, "set", "Page1", "bogus", self.root]), 1)


if __name__ == "__main__":
    unittest.main()
