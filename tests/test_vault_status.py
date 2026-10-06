"""vault_status.py: read-only status report for the setup skill."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "plugin", "scripts"))
import vault_status  # noqa: E402


def make_project(vault=None):
    root = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", root], check=True)
    if vault:
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "artifact-vault.json"), "w") as fh:
            json.dump({"vault": vault}, fh)
    return root


def spool_version(data, key, art, ver, seq=1):
    folder = os.path.join(data, "spool", key, art, ver)
    os.makedirs(folder)
    with open(os.path.join(folder, "meta.json"), "w") as fh:
        json.dump({"id": art, "version": ver, "seq": seq}, fh)
    with open(os.path.join(data, "spool", key, "vault.json"), "w") as fh:
        json.dump({"vault": "git@example.com:org/example-artifact.git", "name": "example-artifact"}, fh)


def status(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        vault_status.main(["vault_status.py", *argv])
    return out.getvalue()


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.data = os.path.realpath(tempfile.mkdtemp())

    def test_project_without_vault(self):
        text = status(["--data", self.data, "--author", "a@example.com", make_project()])
        self.assertIn("This project: no vault", text)
        self.assertIn("All good.", text)

    def test_project_with_vault_and_pending_pages(self):
        root = make_project("git@example.com:org/example-artifact.git")
        spool_version(self.data, "k1", "Art1", "v1")
        spool_version(self.data, "k1", "Art2", "v1")
        os.makedirs(os.path.join(self.data, "state"))
        with open(os.path.join(self.data, "state", "k1-push.json"), "w") as fh:
            json.dump({"ok": True, "at": "2026-10-06T00:00:00+00:00"}, fh)
        text = status(["--data", self.data, "--author", "a@example.com", root])
        self.assertIn("archives into example-artifact", text)
        self.assertIn("Vault example-artifact: 2 waiting; last push OK", text)

    def test_failed_push_and_missing_author_are_listed_to_fix(self):
        spool_version(self.data, "k1", "Art1", "v1")
        os.makedirs(os.path.join(self.data, "state"))
        with open(os.path.join(self.data, "state", "k1-push.json"), "w") as fh:
            json.dump({"ok": False, "at": "2026-10-06T00:00:00+00:00", "error": "RuntimeError: offline"}, fh)
        text = status(["--data", self.data, "--author", "${user_config.author_email}", make_project()])
        self.assertIn("last push FAILED", text)
        self.assertIn("To fix:", text)
        self.assertIn("RuntimeError: offline", text)
        self.assertIn("Set the author email", text)

    def test_unsubstituted_data_dir_is_reported(self):
        text = status(["--data", "${CLAUDE_PLUGIN_DATA}", make_project()])
        self.assertIn("plugin data folder is unknown", text)

    def test_reports_plugin_version(self):
        self.assertIn("Plugin: artifact-vault ", status(["--data", self.data, make_project()]))

    def test_reading_does_not_write(self):
        spool_version(self.data, "k1", "Art1", "v1")
        before = sorted(os.walk(self.data))
        status(["--data", self.data, make_project()])
        self.assertEqual(sorted(os.walk(self.data)), before)


if __name__ == "__main__":
    unittest.main()
