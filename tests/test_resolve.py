import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "scripts"))
import resolve  # noqa: E402

VAULT = "git@github.com:example-org/example-artifact.git"


def git(root, *a):
    subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)


def make_project(config=None, origin="git@github.com:example-org/example-project.git"):
    root = os.path.realpath(tempfile.mkdtemp())
    git(root, "init", "-q")
    if origin:
        git(root, "remote", "add", "origin", origin)
    if config is not None:
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "artifact-vault.json"), "w") as fh:
            fh.write(config if isinstance(config, str) else json.dumps(config))
    return root


def snapshot(root):
    return sorted((d, tuple(sorted(f))) for d, _, f in os.walk(root) if ".git" not in d.split(os.sep))


class ResolveTest(unittest.TestCase):
    def test_config_gives_vault(self):
        root = make_project({"vault": VAULT})
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": "/tmp/plugin-data"}):
            got = resolve.resolve(root)
        self.assertEqual(got["vault"], VAULT)
        self.assertEqual(got["name"], "example-artifact")
        self.assertRegex(got["clone"], r"^/tmp/plugin-data/vaults/example-artifact-[0-9a-f]{10}$")
        self.assertEqual(got["repo"], "example-project")
        self.assertEqual(got["project"], root)

    def test_subdirectory_resolves_to_repo_root(self):
        root = make_project({"vault": VAULT})
        sub = os.path.join(root, "src", "deep")
        os.makedirs(sub)
        self.assertEqual(resolve.resolve(sub)["project"], root)

    def test_no_config_is_no_vault(self):
        self.assertIsNone(resolve.resolve(make_project()))

    def test_not_a_git_repo_is_no_vault(self):
        self.assertIsNone(resolve.resolve(os.path.realpath(tempfile.mkdtemp())))

    def test_worktree_resolves_to_same_vault_and_repo(self):
        root = make_project({"vault": VAULT})
        git(root, "add", "-A")
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
                       cwd=root, check=True, capture_output=True)
        wt = os.path.realpath(tempfile.mkdtemp()) + "/example-project-feature"
        git(root, "worktree", "add", "-q", wt)
        got = resolve.resolve(wt)
        self.assertEqual(got["vault"], VAULT)
        self.assertEqual(got["repo"], "example-project")
        self.assertEqual(got["project"], wt)

    def test_accepted_url_forms(self):
        for v in ["alice@example.com:team/vault.git", "vault-host:team/vault.git",
                  "ssh://git@example.com/team/vault.git", "https://example.com/team/vault.git",
                  "file:///tmp/vault.git"]:
            got = resolve.resolve(make_project({"vault": v}))
            self.assertIsNotNone(got, v)
            self.assertEqual(got["name"], "vault", v)

    def test_local_absolute_path_vault_is_allowed(self):
        got = resolve.resolve(make_project({"vault": "/tmp/vault-sandbox.git"}))
        self.assertEqual(got["name"], "vault-sandbox")

    def test_invalid_configs_are_no_vault_with_reason(self):
        for cfg in ["{not json", {"vault": ""}, {"other": 1}, ["x"], {"vault": "relative/path"}]:
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertIsNone(resolve.resolve(make_project(cfg)), cfg)
            self.assertIn("ignoring", err.getvalue())

    def test_same_repo_name_different_owner_gets_different_clone(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": "/tmp/plugin-data"}):
            a = resolve.resolve(make_project({"vault": "git@github.com:owner-a/artifacts.git"}))
            b = resolve.resolve(make_project({"vault": "git@github.com:owner-b/artifacts.git"}))
        self.assertEqual(a["name"], b["name"])
        self.assertNotEqual(a["clone"], b["clone"])

    def test_dot_paths_stay_inside_vaults_folder(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": "/tmp/plugin-data"}):
            for v in ["/tmp/x/.", "/tmp/x/child/..", "/"]:
                got = resolve.resolve(make_project({"vault": v}))
                parent, leaf = os.path.split(got["clone"])
                self.assertEqual(parent, "/tmp/plugin-data/vaults", v)
                self.assertNotIn(leaf.split("-")[0], ("", ".", ".."), v)

    def test_repo_without_origin_uses_folder_name(self):
        root = make_project({"vault": VAULT}, origin=None)
        self.assertEqual(resolve.resolve(root)["repo"], os.path.basename(root))

    def test_cli_prints_json_or_no_vault(self):
        for root, expect in [(make_project({"vault": VAULT}), VAULT), (make_project(), None)]:
            out = io.StringIO()
            with redirect_stdout(out):
                resolve.main(["resolve.py", root])
            text = out.getvalue().strip()
            if expect:
                self.assertEqual(json.loads(text)["vault"], expect)
            else:
                self.assertEqual(text, "NO_VAULT")

    def test_does_not_write_anything(self):
        root = make_project({"vault": VAULT})
        before = snapshot(root)
        resolve.resolve(root)
        self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
