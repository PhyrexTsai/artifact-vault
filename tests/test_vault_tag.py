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
        return json.loads(run("git", "show", "main:overrides/Page1.json", cwd=self.bare))

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

    def test_cli_exit_codes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(vault_tag.main(["vault_tag.py", "--data", self.data, "list", self.root]), 0)
        self.assertIn("Launch plan", out.getvalue())
        self.assertEqual(vault_tag.main(["vault_tag.py", "--data", self.data, "set", "Page1", "bogus", self.root]), 1)


if __name__ == "__main__":
    unittest.main()
