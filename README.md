# artifact-vault

Claude Code plugin that archives published artifacts into a git-backed library repo.

A project opts in with a `.claude/artifact-vault.json` file that names its library repo. In projects without that file, the plugin does nothing.

> Status: early development. The hooks are placeholders until capture and push are implemented.

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
