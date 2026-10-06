import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import leak_scan  # noqa: E402

NOREPLY = "1+tester@users.noreply.github.com"
SECRET = "acme-secret-project"


def make_repo(files, email=NOREPLY, message="init"):
    root = tempfile.mkdtemp()
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
    run("init", "-q")
    for path, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, path)) or root, exist_ok=True)
        with open(os.path.join(root, path), "w") as fh:
            fh.write(text)
    run("add", "-A")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": email}
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True, capture_output=True, env={**os.environ, **env})
    return root


def scan(root, patterns=SECRET, ci="false"):
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"LEAK_PATTERNS": patterns, "CI": ci}), redirect_stdout(out):
        code = leak_scan.main(root)
    return code, out.getvalue()


class LeakScanTest(unittest.TestCase):
    def test_clean_repo_passes(self):
        code, out = scan(make_repo({"README.md": "hello\n"}))
        self.assertEqual(code, 0, out)

    def test_private_pattern_in_file_fails_without_printing_it(self):
        code, out = scan(make_repo({"docs/a.md": "line one\nsee ACME-Secret-Project here\n"}))
        self.assertEqual(code, 1)
        self.assertIn("docs/a.md:2", out)
        self.assertNotIn(SECRET, out.lower())

    def test_builtin_local_path_fails(self):
        code, out = scan(make_repo({"a.json": '{"cwd": "/Use' + 'rs/someone/x"}\n'}))
        self.assertEqual(code, 1)
        self.assertIn("a.json:1", out)

    def test_private_pattern_in_commit_message_fails(self):
        code, out = scan(make_repo({"a.txt": "x\n"}, message=f"fix {SECRET} thing"))
        self.assertEqual(code, 1)
        self.assertIn("message matches", out)
        self.assertNotIn(SECRET, out.lower())

    def test_non_noreply_author_fails(self):
        code, out = scan(make_repo({"a.txt": "x\n"}, email="someone@example.com"))
        self.assertEqual(code, 1)
        self.assertIn("not a GitHub noreply address", out)
        self.assertNotIn("someone@example.com", out)

    def test_ci_without_patterns_fails_closed(self):
        root = make_repo({"a.txt": "x\n"})
        with self.assertRaises(SystemExit):
            scan(root, patterns="", ci="true")

    def test_this_repo_scanner_does_not_match_itself(self):
        here = os.path.join(os.path.dirname(__file__), "..", "scripts", "leak_scan.py")
        with open(here) as fh:
            text = fh.read().lower()
        for p in leak_scan.BUILTIN:
            self.assertNotIn(p.lower(), text)


if __name__ == "__main__":
    unittest.main()
