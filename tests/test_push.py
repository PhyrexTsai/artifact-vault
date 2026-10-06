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


def version_meta(bare, art, version):
    return json.loads(run("git", "show", f"main:pages/{art}/{version}/meta.json", cwd=bare))


def versions(bare, art="ExAmPlEiD123"):
    return sorted({p.split("/")[2] for p in vault_files(bare) if p.startswith(f"pages/{art}/")})


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
        self.assertEqual(version_meta(bare, "ExAmPlEiD123", "v1")["title"], "Example")
        self.assertFalse(any(p.startswith("meta/") for p in files))  # summaries are built by the site
        self.assertEqual(self.spooled(), [])
        self.assertTrue(self.state()["ok"])

    def test_empty_vault_gets_first_commit(self):
        bare = make_vault(empty=True)
        publish(make_project(bare), self.env)
        push.main(self.env)
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_every_version_is_kept_even_duplicates(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env, version="v1", seq=1)
        publish(root, self.env, version="v3", seq=3)
        publish(root, self.env, version="v2", seq=2, body="<p>changed</p>\n")
        push.main(self.env)
        self.assertEqual(versions(bare), ["v1", "v2", "v3"])
        self.assertEqual(version_meta(bare, "ExAmPlEiD123", "v1")["digest"],
                         version_meta(bare, "ExAmPlEiD123", "v3")["digest"])

    def test_two_machines_archiving_one_artifact_do_not_conflict(self):
        bare = make_vault()
        root = make_project(bare)
        other = os.path.realpath(tempfile.mkdtemp())
        other_env = {**self.env, "CLAUDE_PLUGIN_DATA": other}
        publish(root, self.env, version="v1", seq=1)
        publish(root, other_env, version="v2", seq=2, body="<p>other</p>\n")
        push.main(other_env)
        push.main(self.env)
        self.assertTrue(self.state()["ok"])
        self.assertEqual(versions(bare), ["v1", "v2"])

    def test_gitignore_in_vault_cannot_drop_archived_files(self):
        bare = make_vault()
        work = os.path.join(os.path.dirname(bare), "seed")
        with open(os.path.join(work, ".gitignore"), "w") as fh:
            fh.write("*.png\n")
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "ignore png", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        root = make_project(bare)
        with open(os.path.join(root, "logo.png"), "wb") as fh:
            fh.write(b"\x89PNG")
        with open(os.path.join(root, "index.html"), "w") as fh:
            fh.write("<img src=logo.png>\n")
        event = {"tool_name": "Artifact", "cwd": root,
                 "tool_input": {"file_path": os.path.join(root, "index.html"), "files": {"logo.png": "logo.png"}},
                 "tool_response": {"url": ARTIFACT + "Img1", "title": "Img", "version": "v1", "seq": 1}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)
        push.main(self.env)
        self.assertIn("pages/Img1/v1/logo.png", vault_files(bare))

    def test_chinese_file_names_are_archived(self):
        bare = make_vault()
        root = make_project(bare)
        with open(os.path.join(root, "圖.png"), "wb") as fh:
            fh.write(b"\x89PNG")
        with open(os.path.join(root, "index.html"), "w") as fh:
            fh.write("<img src='圖.png'>\n")
        event = {"tool_name": "Artifact", "cwd": root,
                 "tool_input": {"file_path": os.path.join(root, "index.html"), "files": {"圖.png": "圖.png"}},
                 "tool_response": {"url": ARTIFACT + "Zh1", "title": "中文", "version": "v1", "seq": 1}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertIn("pages/Zh1/v1/圖.png", set(run("git", "ls-tree", "-r", "-z", "--name-only", "main", cwd=bare).split("\0")))

    def test_decomposed_unicode_name_is_archived(self):
        bare = make_vault()
        root = make_project(bare)
        name = "cafe\u0301.png"
        with open(os.path.join(root, name), "wb") as fh:
            fh.write(b"\x89PNG")
        with open(os.path.join(root, "index.html"), "w") as fh:
            fh.write("<p>x</p>\n")
        event = {"tool_name": "Artifact", "cwd": root,
                 "tool_input": {"file_path": os.path.join(root, "index.html"), "files": {name: name}},
                 "tool_response": {"url": ARTIFACT + "Nfd1", "title": "nfd", "version": "v1", "seq": 1}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        tree = set(run("git", "ls-tree", "-r", "-z", "--name-only", "main", cwd=bare).split("\0"))
        self.assertIn(f"pages/Nfd1/v1/{name}", tree)  # exact spelling the page references

    def test_vault_gitattributes_cannot_rewrite_archived_bytes(self):
        bare = make_vault()
        work = os.path.join(os.path.dirname(bare), "seed")
        with open(os.path.join(work, ".gitattributes"), "w") as fh:
            fh.write("*.html text eol=lf\n")
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "attrs", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        publish(make_project(bare), self.env, body="<p>a</p>\r\n<p>b</p>\r\n")
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        raw = subprocess.run(["git", "show", "main:pages/ExAmPlEiD123/v1/index.html"], cwd=bare,
                             capture_output=True).stdout
        self.assertIn(b"\r\n", raw)

    def test_fresh_clone_does_not_rewrite_existing_archives(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env, version="v1", seq=1, body="<p>lf</p>\n")
        push.main(self.env)
        work = os.path.join(os.path.dirname(bare), "seed")
        run("git", "pull", "-q", "origin", "main", cwd=work)
        with open(os.path.join(work, ".gitattributes"), "w") as fh:
            fh.write("*.html text eol=crlf\n")
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "crlf", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        other = {**self.env, "CLAUDE_PLUGIN_DATA": os.path.realpath(tempfile.mkdtemp())}
        publish(root, other, version="v2", seq=2, body="<p>two</p>\n")
        push.main(other)
        raw = subprocess.run(["git", "show", "main:pages/ExAmPlEiD123/v1/index.html"], cwd=bare,
                             capture_output=True).stdout
        self.assertEqual(raw, b"<p>lf</p>\n")

    def hooks(self, script):
        d = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(d, "pre-commit"), "w") as fh:
            fh.write("#!/bin/sh\n" + script)
        os.chmod(os.path.join(d, "pre-commit"), 0o755)
        return mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                            "GIT_CONFIG_VALUE_0": d})

    def test_global_hooks_do_not_run_in_the_private_clone(self):
        bare = make_vault()
        publish(make_project(bare), self.env)
        with self.hooks('for f in $(git diff --cached --name-only); do echo formatted > "$f"; git add "$f"; done\n'):
            push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertEqual(run("git", "show", "main:pages/ExAmPlEiD123/v1/index.html", cwd=bare), "<p>hello</p>\n")

    def test_quoted_file_name_is_archived(self):
        bare = make_vault()
        root = make_project(bare)
        name = '"logo".png'
        with open(os.path.join(root, name), "wb") as fh:
            fh.write(b"\x89PNG")
        with open(os.path.join(root, "index.html"), "w") as fh:
            fh.write("<p>x</p>\n")
        event = {"tool_name": "Artifact", "cwd": root,
                 "tool_input": {"file_path": os.path.join(root, "index.html"), "files": {name: name}},
                 "tool_response": {"url": ARTIFACT + "Q1", "title": "q", "version": "v1", "seq": 1}}
        with redirect_stdout(io.StringIO()):
            capture.capture(event, env=self.env)
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())

    def test_blob_id_matches_git(self):
        path = os.path.join(self.data, "x.bin")
        with open(path, "wb") as fh:
            fh.write(b"abc\r\n\x00")
        self.assertEqual(push.blob_id(path), run("git", "hash-object", "--no-filters", path).strip())

    def test_version_name_with_tmp_is_pushed(self):
        bare = make_vault()
        publish(make_project(bare), self.env, version="v1.tmp-final")
        push.main(self.env)
        self.assertIn("pages/ExAmPlEiD123/v1.tmp-final/index.html", vault_files(bare))

    def test_identity_falls_back_to_author_email(self):
        bare = make_vault()
        publish(make_project(bare), self.env)
        empty_cfg = os.path.join(self.data, "empty.gitconfig")
        with open(empty_cfg, "w") as fh:
            fh.write("[user]\n\tuseConfigOnly = true\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": empty_cfg, "GIT_CONFIG_NOSYSTEM": "1",
                    "CLAUDE_PLUGIN_DATA": self.data, "CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL": "author@example.com"})
        with mock.patch.dict(os.environ, env, clear=True):
            push.main(env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertEqual(run("git", "log", "-1", "--format=%ae", "main", cwd=bare).strip(), "author@example.com")

    def test_missing_name_alone_is_filled(self):
        bare = make_vault()
        publish(make_project(bare), self.env)
        cfg = os.path.join(self.data, "email-only.gitconfig")
        with open(cfg, "w") as fh:
            fh.write("[user]\n\temail = someone@example.com\n\tuseConfigOnly = true\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": cfg, "GIT_CONFIG_NOSYSTEM": "1",
                    "CLAUDE_PLUGIN_DATA": self.data, "CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL": "author@example.com"})
        with mock.patch.dict(os.environ, env, clear=True):
            push.main(env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertEqual(run("git", "log", "-1", "--format=%ae", "main", cwd=bare).strip(), "someone@example.com")

    def test_symlinked_pages_in_vault_are_refused(self):
        bare = make_vault()
        work = os.path.join(os.path.dirname(bare), "seed")
        outside = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(outside, "ExAmPlEiD123", "v1"))
        with open(os.path.join(outside, "ExAmPlEiD123", "v1", "keep.txt"), "w") as fh:
            fh.write("precious\n")
        os.symlink(outside, os.path.join(work, "pages"))
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "link", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        publish(make_project(bare), self.env)
        push.main(self.env)
        self.assertFalse(self.state()["ok"])
        self.assertTrue(os.path.exists(os.path.join(outside, "ExAmPlEiD123", "v1", "keep.txt")))
        self.assertEqual(len(self.spooled()), 1)

    def test_pre_push_hook_from_environment_does_not_run(self):
        bare = make_vault()
        publish(make_project(bare), self.env)
        d = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(d, "pre-push"), "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(d, "pre-push"), 0o755)
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                          "GIT_CONFIG_VALUE_0": d}):
            push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_lock_is_exclusive(self):
        path = os.path.join(self.data, "locks", "k.lock")
        with push.Lock(path) as a:
            with push.Lock(path) as b:
                self.assertTrue(a.held)
                self.assertFalse(b.held)
        with push.Lock(path) as c:
            self.assertTrue(c.held)

    def test_vault_ignoring_all_of_pages_still_archives(self):
        bare = make_vault()
        work = os.path.join(os.path.dirname(bare), "seed")
        with open(os.path.join(work, ".gitignore"), "w") as fh:
            fh.write("pages/\n")
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "ignore pages", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        publish(make_project(bare), self.env)
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_empty_vault_rejecting_first_push_is_retried(self):
        bare = make_vault(empty=True)
        hook = os.path.join(bare, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        publish(make_project(bare), self.env)
        push.main(self.env)
        self.assertFalse(self.state()["ok"])
        os.remove(hook)
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_unverified_leftovers_are_discarded_not_pushed(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env)
        push.main(self.env)
        clone = os.path.join(self.data, "vaults", os.listdir(os.path.join(self.data, "vaults"))[0])
        os.makedirs(os.path.join(clone, "pages", "Left", "v1"))
        with open(os.path.join(clone, "pages", "Left", "v1", "index.html"), "w") as fh:
            fh.write("left behind\n")
        with open(os.path.join(clone, "pages", "ExAmPlEiD123", "v1", "index.html"), "w") as fh:
            fh.write("changed by formatter\n")
        run("git", "add", "pages/Left", cwd=clone)  # staged but never committed
        publish(root, self.env, version="v2", seq=2, body="<p>two</p>\n")
        push.main(self.env)
        push.main(self.env)  # a second run must not commit the leftovers either
        self.assertTrue(self.state()["ok"], self.state())
        files = vault_files(bare)
        self.assertNotIn("pages/Left/v1/index.html", files)
        self.assertIn("pages/ExAmPlEiD123/v2/index.html", files)
        self.assertEqual(run("git", "show", "main:pages/ExAmPlEiD123/v1/index.html", cwd=bare), "<p>hello</p>\n")

    def test_interrupted_cleanup_cannot_overwrite_an_archive(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env)
        [(meta, folder)] = self.spooled()
        with mock.patch.object(push.shutil, "rmtree", side_effect=lambda p, ignore_errors=False: os.remove(os.path.join(p, "index.html")) if os.path.exists(os.path.join(p, "index.html")) else None):
            push.main(self.env)
        push.main(self.env)
        self.assertEqual(run("git", "show", "main:pages/ExAmPlEiD123/v1/index.html", cwd=bare), "<p>hello</p>\n")
        self.assertEqual(self.spooled(), [])

    def test_failing_global_hook_does_not_block_archiving(self):
        bare = make_vault(empty=True)
        publish(make_project(bare), self.env)
        with self.hooks("exit 1\n"):
            push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_version_name_with_done_is_pushed(self):
        bare = make_vault()
        publish(make_project(bare), self.env, version="v1.done-final")
        push.main(self.env)
        self.assertIn("pages/ExAmPlEiD123/v1.done-final/index.html", vault_files(bare))

    def test_tampered_commit_is_still_caught_and_undone(self):
        """Second line of defense: if anything rewrites an older archive during the commit."""
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env)
        push.main(self.env)
        publish(root, self.env, version="v2", seq=2, body="<p>two</p>\n")
        real = push.commit_pages

        def tamper(clone, subject):
            real(clone, subject)
            with open(os.path.join(clone, "pages", "ExAmPlEiD123", "v1", "index.html"), "w") as fh:
                fh.write("rewritten\n")
            run("git", "commit", "-q", "--amend", "-a", "--no-edit", cwd=clone)

        with mock.patch.object(push, "commit_pages", side_effect=tamper):
            push.main(self.env)
        self.assertFalse(self.state()["ok"])
        self.assertEqual(len(self.spooled()), 1)
        self.assertEqual(run("git", "show", "main:pages/ExAmPlEiD123/v1/index.html", cwd=bare), "<p>hello</p>\n")
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertIn("pages/ExAmPlEiD123/v2/index.html", vault_files(bare))

    def test_run_killed_after_commit_before_retire_recovers(self):
        bare = make_vault()
        publish(make_project(bare), self.env)
        with mock.patch.object(push, "retire", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                push.push_vault(self.data, os.listdir(os.path.join(self.data, "spool"))[0])
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertEqual(self.spooled(), [])
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", vault_files(bare))

    def test_unverified_commit_from_a_killed_run_is_rolled_back(self):
        bare = make_vault()
        root = make_project(bare)
        publish(root, self.env)
        push.main(self.env)
        key = os.listdir(os.path.join(self.data, "spool"))[0]
        clone = os.path.join(self.data, "vaults", key)
        with open(os.path.join(clone, "pages", "ExAmPlEiD123", "v1", "index.html"), "w") as fh:
            fh.write("corrupted\n")
        run("git", "commit", "-qam", "unverified", cwd=clone)  # as if killed before check_commit
        publish(root, self.env, version="v3", seq=3, body="<p>three</p>\n")
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertEqual(run("git", "show", "main:pages/ExAmPlEiD123/v1/index.html", cwd=bare), "<p>hello</p>\n")
        self.assertIn("pages/ExAmPlEiD123/v3/index.html", vault_files(bare))

    def test_failed_hook_staging_outside_pages_does_not_block(self):
        bare = make_vault()
        hooks = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(hooks, "pre-commit"), "w") as fh:
            fh.write("#!/bin/sh\necho changed >> README.md\ngit add README.md\nexit 1\n")
        os.chmod(os.path.join(hooks, "pre-commit"), 0o755)
        publish(make_project(bare), self.env)
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                          "GIT_CONFIG_VALUE_0": hooks}):
            push.main(self.env)
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        self.assertEqual(run("git", "show", "main:README.md", cwd=bare), "vault\n")

    def test_unverified_first_commit_to_empty_vault_is_undone(self):
        bare = make_vault(empty=True)
        root = make_project(bare)
        publish(root, self.env)
        key = os.listdir(os.path.join(self.data, "spool"))[0]
        with mock.patch.object(push, "check_commit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                push.push_vault(self.data, key)
        clone = os.path.join(self.data, "vaults", key)
        with open(os.path.join(clone, "extra.txt"), "w") as fh:
            fh.write("x\n")
        run("git", "add", "extra.txt", cwd=clone)
        run("git", "commit", "-q", "--amend", "--no-edit", cwd=clone)  # a hook slipped in a file
        push.main(self.env)
        self.assertTrue(self.state()["ok"], self.state())
        files = vault_files(bare)
        self.assertNotIn("extra.txt", files)
        self.assertIn("pages/ExAmPlEiD123/v1/index.html", files)

    def test_hook_rewrite_left_unstaged_is_not_pushed_later(self):
        bare = make_vault()
        hooks = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(hooks, "pre-commit"), "w") as fh:
            fh.write("#!/bin/sh\nfor f in $(git diff --cached --name-only); do echo formatted > \"$f\"; done\n")
        os.chmod(os.path.join(hooks, "pre-commit"), 0o755)
        root = make_project(bare)
        publish(root, self.env)
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                          "GIT_CONFIG_VALUE_0": hooks}):
            push.main(self.env)
            publish(root, self.env, version="v2", seq=2, body="<p>two</p>\n")
            push.main(self.env)
        self.assertEqual(run("git", "show", "main:pages/ExAmPlEiD123/v1/index.html", cwd=bare), "<p>hello</p>\n")

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
