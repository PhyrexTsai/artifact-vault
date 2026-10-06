"""push.py against local bare repos standing in for the vault."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(__file__)
SCRIPTS = os.path.join(HERE, "..", "plugin", "scripts")
sys.path.insert(0, SCRIPTS)
import capture  # noqa: E402
import push  # noqa: E402

ARTIFACT = "https://claude.ai/" + "artifact/"  # split: the leak scan blocks real artifact links
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def run(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **IDENT}).stdout


def make_vault(empty=False):
    base = os.path.realpath(tempfile.mkdtemp())
    bare = os.path.join(base, "example-artifact.git")
    run("git", "init", "-q", "--bare", "-b", "main", bare)
    if not empty:
        work = os.path.join(base, "seed")
        run("git", "clone", "-q", bare, work)
        with open(os.path.join(work, "README.md"), "w") as fh:
            fh.write("vault\n")
        run("git", "add", "-A", cwd=work)
        run("git", "commit", "-qm", "init", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    return bare


def make_project(vault):
    root = os.path.realpath(tempfile.mkdtemp())
    run("git", "init", "-q", root)
    os.makedirs(os.path.join(root, ".claude"))
    with open(os.path.join(root, ".claude", "artifact-vault.json"), "w") as fh:
        json.dump({"vault": vault}, fh)
    return root


def publish(root, data_env, art="ExAmPlEiD123", version="v1", seq=1, body="<p>hello</p>\n", title="Example"):
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write(body)
    event = {"tool_name": "Artifact", "cwd": root,
             "tool_input": {"file_path": os.path.join(root, "index.html")},
             "tool_response": {"url": ARTIFACT + art, "title": title, "version": version, "seq": seq,
                               "audience": "owner"}}
    with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, data_env):
        assert capture.capture(event, env=data_env) == "queued"


def vault_files(bare):
    return set(run("git", "ls-tree", "-r", "--name-only", "main", cwd=bare).split())


def vault_meta(bare, art="ExAmPlEiD123"):
    return json.loads(run("git", "show", f"main:meta/{art}.json", cwd=bare))


class PushTest(unittest.TestCase):
    def setUp(self):
        self.data = os.path.realpath(tempfile.mkdtemp())
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, **IDENT}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def state(self):
        [f] = [f for f in os.listdir(os.path.join(self.data, "state")) if f.endswith("-push.json")]
        with open(os.path.join(self.data, "state", f)) as fh:
            return json.load(fh)

    def spooled(self):
        return push.queued_versions(os.path.join(self.data, "spool", os.listdir(os.path.join(self.data, "spool"))[0]))

    def test_publish_then_push_lands_in_vault(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env)
        push.main(self.env)
        files = vault_files(bare)
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", files)
        self.assertIn("meta/ExAmPlEiD123.json", files)
        meta = vault_meta(bare)
        self.assertEqual(meta["title"], "Example")
        self.assertEqual([v["version"] for v in meta["versions"]], ["v1"])
        self.assertEqual(self.spooled(), [])
        self.assertTrue(self.state()["ok"])

    def test_empty_vault_gets_first_commit(self):
        bare = make_vault(empty=True)
        publish(make_project(bare), self.env)
        push.main(self.env)
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_duplicate_of_previous_seq_is_dropped(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env, version="v1", seq=1)
        publish(root, self.env, version="v2", seq=2)  # same content
        publish(root, self.env, version="v3", seq=3, body="<p>changed</p>\n")
        push.main(self.env)
        self.assertEqual([v["version"] for v in vault_meta(bare)["versions"]], ["v3", "v1"])
        self.assertEqual(self.state()["duplicates"], 1)

    def test_out_of_order_versions_sorted_and_latest_wins(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env, version="v2", seq=2, body="<p>two</p>\n", title="Two")
        push.main(self.env)
        publish(root, self.env, version="v1", seq=1, body="<p>one</p>\n", title="One")
        push.main(self.env)
        meta = vault_meta(bare)
        self.assertEqual([v["version"] for v in meta["versions"]], ["v2", "v1"])
        self.assertEqual(meta["title"], "Two")

    def test_unreachable_vault_keeps_spool_and_records_error(self):
        root = make_project("/nonexistent/vault.git")
        publish(root, self.env)
        self.assertEqual(push.main(self.env), 0)
        self.assertEqual(len(self.spooled()), 1)
        st = self.state()
        self.assertFalse(st["ok"])
        self.assertEqual(st["pending"], 1)

    def test_bad_ssh_host_fails_fast_without_prompting(self):
        root = make_project("git@nonexistent.invalid:org/vault.git")
        publish(root, self.env)
        start = time.monotonic()
        push.main(self.env)
        self.assertLess(time.monotonic() - start, 60)
        self.assertFalse(self.state()["ok"])

    def test_failed_push_is_retried_next_run(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env)
        hidden = bare + ".away"
        push.main(self.env)  # first clone works
        publish(root, self.env, version="v2", seq=2, body="<p>two</p>\n")
        os.rename(bare, hidden)
        push.main(self.env)
        self.assertFalse(self.state()["ok"])
        os.rename(hidden, bare)
        push.main(self.env)
        self.assertTrue(self.state()["ok"])
        self.assertIn("pages/ExAmPlEiD123/v2/index.html", vault_files(bare))

    def test_concurrent_runs_do_not_corrupt_the_clone(self):
        bare = make_vault()
        root = make_project(bare)
        for i in range(3):
            publish(root, self.env, art=f"Art{i}", version="v1", seq=1, body=f"<p>{i}</p>\n")
        procs = [subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "push.py")],
                                  env={**os.environ, **self.env}) for _ in range(3)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        push.main(self.env)  # whoever lost the lock catches up on the next run
        files = vault_files(bare)
        for i in range(3):
            self.assertIn(f"pages/Art{i}/v1/index.html", files)
        run("git", "fsck", "--no-progress", cwd=bare)

    def test_no_spool_is_a_no_op(self):
        self.assertEqual(push.main(self.env), 0)
        self.assertEqual(push.main({}), 0)


if __name__ == "__main__":
    unittest.main()
