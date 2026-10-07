"""Remove a page from the library for good: from the branch, from all of its history, and from
this machine's queue. Use it for a page that must not be kept (a leaked secret, personal data).

  python3 vault_purge.py [--data <dir>] [--author <email>] plan <id or link> [--in <dir>]
      Read only. Prints what would be removed: commits that touch the page, files at HEAD,
      versions waiting in this machine's spool.

  python3 vault_purge.py [--data <dir>] [--author <email>] run <id or link> --confirm <id> [--in <dir>]
      Rewrites the vault's history without pages/<id> and overrides/<id>, adds
      purged/<id>.json (so no machine pushes the page again), and force-pushes with a lease.
      --confirm must repeat the id exactly.

What a purge cannot reach, and the skill tells the user: other clones of the vault repo (each
must reset to the new branch), GitHub's copies outside the branch (pull request refs, forks,
cached views; GitHub Support removes those), and anything people downloaded or deployed. The
library site needs a redeploy to drop the page.
"""
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push  # noqa: E402
import vault_page  # noqa: E402
import vault_tag  # noqa: E402


class PurgeError(Exception):
    pass


def paths_for(art):
    return [f"pages/{art}", f"overrides/{art}"]


def history(clone, art):
    """[(short sha, subject)] of commits on the branch that touch the page."""
    if not push.head(clone):
        return []  # the vault has no commits yet
    # --full-history: without it git simplifies merges away and misses a side branch that held
    # the page even though the merge result does not.
    out = push.git("log", "--full-history", "--format=%h %s", "--", *paths_for(art), cwd=clone)
    return [tuple(line.split(" ", 1)) if " " in line else (line, "") for line in out.splitlines() if line]


def queued(spool, art):
    folder = os.path.join(spool, art)
    return sorted(os.listdir(folder)) if os.path.isdir(folder) and not os.path.islink(folder) else []


def plan(cwd, ref, env=os.environ):
    art = vault_tag.parse_id(ref)
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise PurgeError("this project has no vault")
        spool = os.path.join(env["CLAUDE_PLUGIN_DATA"], "spool", found["key"])
        files = push.git("ls-tree", "-r", "--name-only", "HEAD", "--", *paths_for(art), cwd=clone).split() \
            if push.head(clone) else []
        title = vault_tag.artifacts(clone).get(art, {}).get("title")
        return {"id": art, "title": title, "vault": found["name"], "commits": history(clone, art),
                "files": len(files), "queued": queued(spool, art),
                "already_purged": art in push.purged_ids(clone)}


def run(cwd, ref, confirm, author=None, env=os.environ):
    art = vault_tag.parse_id(ref)
    if confirm != art:
        raise PurgeError(f"--confirm must repeat the id exactly ({art}); nothing changed")
    data = env["CLAUDE_PLUGIN_DATA"]
    with vault_page.locked_vault(cwd, env) as (found, clone):
        if not found:
            raise PurgeError("this project has no vault")
        spool = os.path.join(data, "spool", found["key"])
        trash = os.path.join(data, "trash")
        for ver in queued(spool, art):  # never pushed: just drop them
            push.retire(os.path.join(spool, art, ver), trash)
        branch = push.current_branch(clone)
        lease = push.rev(clone, f"refs/remotes/origin/{branch}") or ""  # "": the branch must not exist yet
        others = push.git("log", "--full-history", "--format=%H", "--", ".", *[f":(exclude){p}" for p in paths_for(art)],
                          cwd=clone).strip() if push.head(clone) else ""
        if history(clone, art) and not others:
            # Every commit only touched this page: nothing would be left to rewrite onto.
            # Start the branch over with a single root commit (the marker below).
            push.git("checkout", "-q", "--orphan", "purge-tmp", cwd=clone)
            push.git("rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", ".", cwd=clone)
            push.git("clean", "-q", "-f", "-d", "-x", cwd=clone)
            push.git("branch", "-D", branch, cwd=clone)
            push.git("branch", "-m", branch, cwd=clone)
        elif history(clone, art):
            fenv = dict(push.git_env(), FILTER_BRANCH_SQUELCH_WARNING="1")
            # art matched [A-Za-z0-9-]+ in full (parse_id), so it is safe inside the shell command.
            rm = "git rm -r -q --cached --ignore-unmatch -- " + " ".join(paths_for(art))
            r = subprocess.run(["git", *push.NO_HOOKS, "filter-branch", "-f", "--index-filter", rm, "--prune-empty",
                                "--", branch], cwd=clone, env=fenv, capture_output=True, text=True, timeout=600)
            if r.returncode:
                push.mirror_remote(clone)
                raise PurgeError(f"rewriting history failed; nothing pushed: {(r.stderr or r.stdout).strip()[-300:]}")
        marker = f"purged/{art}.json"
        path = os.path.join(clone, *marker.split("/"))
        push.inside_clone(clone, path)
        if not os.path.exists(path):
            push.write_json(path, {"id": art, "by": author or None,
                                   "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
            push.git("add", "-f", "--", marker, cwd=clone)
            push.git("commit", "--quiet", "-m", f"purge: {art}", cwd=clone)  # the id only: a title may be the secret
        if push.git("log", "--full-history", "--format=%h", "HEAD", "--", *paths_for(art), cwd=clone).strip():
            push.mirror_remote(clone)
            raise PurgeError("the page is still in the rewritten branch; nothing pushed")
        try:
            push.git("push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{lease}", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
        except RuntimeError as e:
            push.mirror_remote(clone)
            raise PurgeError(f"the vault changed while purging, or the push was refused; run the purge again ({e})")
        push.git("fetch", "--quiet", "origin", cwd=clone)
        if push.rev(clone, f"refs/remotes/origin/{branch}") != push.head(clone):
            raise PurgeError("pushed, but the remote branch does not match; check the vault")
        # Now that no ref points at the old history, drop its objects from this clone too.
        for ref_line in push.git("for-each-ref", "--format=%(refname)", "refs/original", cwd=clone).splitlines():
            push.git("update-ref", "-d", ref_line, cwd=clone)
        push.git("reflog", "expire", "--expire=now", "--all", cwd=clone)
        push.git("gc", "--quiet", "--prune=now", cwd=clone, timeout=600)
        if push.git("log", "--all", "--full-history", "--format=%h", "--", *paths_for(art), cwd=clone).strip():
            raise PurgeError("pushed, but this clone still holds the page; delete the clone folder to be sure")
        return {"id": art, "vault": found["name"], "branch": branch, "head": push.head(clone)[:12]}


def main(argv):
    args = argv[1:]
    env = dict(os.environ)
    author = env.get("CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL", "")
    while args and args[0] in ("--data", "--author") and len(args) >= 2:
        if args[1] and "${" not in args[1]:
            if args[0] == "--data":
                env["CLAUDE_PLUGIN_DATA"] = args[1]
            else:
                author = env["CLAUDE_PLUGIN_OPTION_AUTHOR_EMAIL"] = args[1]
        args = args[2:]
    os.environ.update(env)
    opts, rest, it = {}, [], iter(args[1:])
    for a in it:
        if a in ("--confirm", "--in"):
            opts[a[2:]] = next(it, "")
        else:
            rest.append(a)
    cwd = opts.get("in") or os.getcwd()
    try:
        if args[:1] == ["plan"] and len(rest) == 1:
            print(json.dumps(plan(cwd, rest[0], env), ensure_ascii=False, indent=2))
            return 0
        if args[:1] == ["run"] and len(rest) == 1 and "confirm" in opts:
            print(json.dumps(run(cwd, rest[0], opts["confirm"], author, env), ensure_ascii=False))
            return 0
    except (PurgeError, vault_tag.TagError, vault_page.VaultBusy, RuntimeError) as e:
        print(f"artifact-vault: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
