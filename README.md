# artifact-vault

Claude Code plugin that archives published artifacts into a git-backed library repo.

A project opts in with a `.claude/artifact-vault.json` file that names its library repo. In projects without that file, the plugin does nothing.

Every publish in an opted-in project is queued by a hook and pushed to the library in the background at the end of the turn.

## Skills

| Skill | What it does |
|---|---|
| `page` | Starts a page from one of the library's templates. |
| `tag` | Changes the category of an archived page. |
| `backfill` | Reads pages published earlier back from claude.ai and adds them. |
| `purge` | Removes a page from the library and from all of its history. |
| `setup` | Reports the library, the last push, and anything waiting. |

### Backfill: one known limit

A read-back page carries no publish count, so a backfilled version is numbered after the versions the library already has. If another machine holds an unpushed capture of the same page that is older than the live version, it arrives later with a higher number and sorts as the newest. Before backfilling a page that was also published from another machine, let that machine push first (open Claude Code there, or run `/artifact-vault:setup`). Pages published before the project used a library, the usual case, are not affected.

### Purge: what it cannot reach

A purge rewrites the library repo's history without the page, adds `purged/<id>.json`, and force-pushes. Afterwards:

- Redeploy the library site if it does not deploy on push.
- Every other clone of the library repo must run `git fetch origin && git reset --hard origin/<branch>`; a `git pull` merges the old history back. Plugin clones on other machines follow the rewrite by themselves once they run this version.
- GitHub keeps copies outside the branch (pull request refs, forks, cached views) until GitHub Support removes them.
- Rotate any secret the page held.

## When someone leaves

Remove their access in every place that grants it:

1. The library site's allowlist (for example an `ALLOWED_EMAILS` config var), then restart the site. Signed-in sessions end at their next request.
2. The sign-in provider's list of test users or allowed accounts (for example Google OAuth test users).
3. The GitHub organization or repository access for the library repo, and for any project repo that has a `.claude/artifact-vault.json`.
4. Anything they published stays in the library; purge pages only when they must not be kept.

## Install

```
claude plugin marketplace add PhyrexTsai/artifact-vault
claude plugin install artifact-vault@artifact-vault
```

## Development

```
claude plugin validate --strict ./plugin
claude plugin validate --strict .
python3 -m unittest discover -s tests -v
LEAK_PATTERNS="$(cat ~/.config/artifact-vault/leak-patterns)" python3 scripts/leak_scan.py
```

This repo is public. CI fails if a tracked file or a commit message contains a private string from the `LEAK_PATTERNS` secret, a local path, or an artifact link, or if a commit is not authored with a GitHub noreply email.
