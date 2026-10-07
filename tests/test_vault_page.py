"""vault_page.py against a local bare repo standing in for a vault with templates."""
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
import vault_page  # noqa: E402

IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
TEMPLATE = """<!--
  guide for the skill, removed on render
-->
<title>Sample</title>
<meta name="vault:type" content="plan">
<style>
/*@@BASE_CSS@@*/
</style>
<div class="brand"><!--@@LOGO@@--></div>
<h1>Sample title</h1>
"""


def run(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **IDENT}).stdout


def make_vault(extra_types=None):
    base = os.path.realpath(tempfile.mkdtemp())
    bare = os.path.join(base, "example-artifact.git")
    run("git", "init", "-q", "--bare", "-b", "main", bare)
    work = os.path.join(base, "seed")
    run("git", "clone", "-q", bare, work)
    os.makedirs(os.path.join(work, "templates"))
    types = {"plan": {"label": "Plan", "template": "templates/plan.html", "diagram": ["archify", "mermaid"]}}
    types.update(extra_types or {})
    files = {"vault.json": json.dumps({"name": "example", "css": "templates/base.css", "logo": "templates/logo.svg",
                                       "types": types}),
             "templates/plan.html": TEMPLATE, "templates/base.css": "body{color:red}",
             "templates/logo.svg": "<svg viewBox='0 0 1 1'></svg>"}
    for p, text in files.items():
        with open(os.path.join(work, p), "w") as fh:
            fh.write(text)
    run("git", "add", "-A", cwd=work)
    run("git", "commit", "-qm", "templates", cwd=work)
    run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    return bare


def make_project(vault=None):
    root = os.path.realpath(tempfile.mkdtemp())
    run("git", "init", "-q", root)
    if vault:
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "artifact-vault.json"), "w") as fh:
            json.dump({"vault": vault}, fh)
    return root


class VaultPageTest(unittest.TestCase):
    def setUp(self):
        self.data = os.path.realpath(tempfile.mkdtemp())
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, **IDENT}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.home = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        self.patch.stop()

    def test_no_config_is_no_vault(self):
        self.assertIsNone(vault_page.prepare(make_project(), self.env, home=self.home))
        out = io.StringIO()
        with redirect_stdout(out):
            vault_page.main(["vault_page.py", "prepare", make_project()])
        self.assertEqual(out.getvalue().strip(), "NO_VAULT")

    def test_data_dir_can_come_from_the_command_line(self):
        root = make_project(make_vault())
        other = os.path.realpath(tempfile.mkdtemp())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": ""}), redirect_stdout(out):
            vault_page.main(["vault_page.py", "--data", other, "prepare", root])
        self.assertIn('"plan"', out.getvalue())
        self.assertTrue(os.path.isdir(os.path.join(other, "vaults")))

    def test_prepare_lists_types_and_falls_back_to_mermaid(self):
        info = vault_page.prepare(make_project(make_vault()), self.env, home=self.home)
        self.assertEqual(list(info["types"]), ["plan"])
        self.assertEqual(info["types"]["plan"]["label"], "Plan")
        self.assertEqual(info["types"]["plan"]["use_diagram"], "mermaid")  # no archify in this HOME
        self.assertEqual(info["diagram_tools"], ["mermaid"])

    def test_archify_is_used_when_installed(self):
        os.makedirs(os.path.join(self.home, ".claude", "skills", "archify"))
        open(os.path.join(self.home, ".claude", "skills", "archify", "SKILL.md"), "w").close()
        with mock.patch.object(vault_page.shutil, "which", return_value="/usr/bin/node"):
            info = vault_page.prepare(make_project(make_vault()), self.env, home=self.home)
        self.assertEqual(info["types"]["plan"]["use_diagram"], "archify")

    def test_render_inlines_css_and_logo_and_drops_guide(self):
        root = make_project(make_vault())
        out = os.path.join(root, "page.html")
        vault_page.render("plan", out, root, self.env)
        page = open(out).read()
        self.assertIn("body{color:red}", page)
        self.assertIn("<svg viewBox='0 0 1 1'></svg>", page)
        self.assertNotIn("guide for the skill", page)
        self.assertNotIn("@@", page)
        self.assertIn('<meta name="vault:type" content="plan">', page)

    def test_render_unknown_type_explains(self):
        root = make_project(make_vault())
        with self.assertRaises(SystemExit) as e:
            vault_page.render("deck", os.path.join(root, "x.html"), root, self.env)
        self.assertIn("plan", str(e.exception))

    def test_template_outside_the_vault_is_ignored(self):
        bad = {"evil": {"template": "../../etc/hosts"}, "missing": {"template": "templates/nope.html"}}
        info = vault_page.prepare(make_project(make_vault(bad)), self.env, home=self.home)
        self.assertEqual(list(info["types"]), ["plan"])

    def test_css_and_logo_outside_the_vault_are_not_embedded(self):
        secret = os.path.join(os.path.realpath(tempfile.mkdtemp()), "secret.txt")
        with open(secret, "w") as fh:
            fh.write("TOP-SECRET")
        bare = make_vault()
        work = os.path.join(os.path.dirname(bare), "seed")
        cfg = json.load(open(os.path.join(work, "vault.json")))
        cfg.update({"css": secret, "logo": "../../../../../../" + secret.lstrip("/")})
        with open(os.path.join(work, "vault.json"), "w") as fh:
            json.dump(cfg, fh)
        run("git", "commit", "-qam", "evil", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        root = make_project(bare)
        out = os.path.join(root, "page.html")
        vault_page.render("plan", out, root, self.env)
        self.assertNotIn("TOP-SECRET", open(out).read())

    def test_git_metadata_is_never_embedded(self):
        bare = make_vault()
        work = os.path.join(os.path.dirname(bare), "seed")
        cfg = json.load(open(os.path.join(work, "vault.json")))
        cfg.update({"css": ".git/config", "logo": "link-to-git"})
        with open(os.path.join(work, "vault.json"), "w") as fh:
            json.dump(cfg, fh)
        os.symlink(".git/config", os.path.join(work, "link-to-git"))
        run("git", "add", "-A", cwd=work); run("git", "commit", "-qm", "evil", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        root = make_project(bare)
        vault_page.prepare(root, self.env, home=self.home)
        clone = os.path.join(self.data, "vaults", os.listdir(os.path.join(self.data, "vaults"))[0])
        run("git", "config", "http.extraHeader", "Authorization: Bearer SYNTHETIC", cwd=clone)
        out = os.path.join(root, "page.html")
        vault_page.render("plan", out, root, self.env)
        self.assertNotIn("SYNTHETIC", open(out).read())

    def test_type_tag_goes_into_head_after_doctype(self):
        page = '<!doctype html><html><head><meta name="description" content="plan"></head><body></body></html>'
        fixed = vault_page.add_type(page, "plan")
        self.assertTrue(fixed.lower().startswith("<!doctype html>"))
        self.assertIn('<head>\n<meta name="vault:type" content="plan">', fixed)

    def test_render_never_overwrites_an_existing_page(self):
        root = make_project(make_vault())
        out = os.path.join(root, "page.html")
        with open(out, "w") as fh:
            fh.write('<meta name="vault:skip">\n<p>mine</p>\n')
        with self.assertRaises(SystemExit):
            vault_page.render("plan", out, root, self.env)
        self.assertIn("mine", open(out).read())

    def test_busy_vault_fails_cleanly_after_waiting(self):
        root = make_project(make_vault())
        vault_page.prepare(root, self.env, home=self.home)
        key = os.listdir(os.path.join(self.data, "vaults"))[0]
        with vault_page.push.Lock(os.path.join(self.data, "locks", f"{key}.lock")):
            with self.assertRaises(vault_page.VaultBusy):
                with vault_page.locked_vault(root, self.env, wait=0.3):
                    pass

    def test_prepare_leaves_the_clone_equal_to_the_remote(self):
        sys.path.insert(0, os.path.join(HERE, "..", "plugin", "scripts"))
        import push
        bare = make_vault()
        root = make_project(bare)
        vault_page.prepare(root, self.env, home=self.home)
        clone = os.path.join(self.data, "vaults", os.listdir(os.path.join(self.data, "vaults"))[0])
        with open(os.path.join(clone, "stray.txt"), "w") as fh:  # leftovers are discarded
            fh.write("x")
        work = os.path.join(os.path.dirname(bare), "seed")
        with open(os.path.join(work, "templates", "base.css"), "w") as fh:
            fh.write("body{color:green}")
        run("git", "commit", "-qam", "green", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        vault_page.prepare(root, self.env, home=self.home)
        self.assertEqual(push.head(clone), run("git", "rev-parse", "main", cwd=bare).strip())
        self.assertFalse(os.path.exists(os.path.join(clone, "stray.txt")))

    def test_prepare_refreshes_templates(self):
        bare = make_vault()
        root = make_project(bare)
        vault_page.prepare(root, self.env, home=self.home)
        work = os.path.join(os.path.dirname(bare), "seed")
        with open(os.path.join(work, "templates", "base.css"), "w") as fh:
            fh.write("body{color:blue}")
        run("git", "commit", "-qam", "blue", cwd=work)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        out = os.path.join(root, "page.html")
        vault_page.render("plan", out, root, self.env)
        self.assertIn("body{color:blue}", open(out).read())


if __name__ == "__main__":
    unittest.main()
