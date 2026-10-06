---
name: setup
description: Check the artifact library (vault) setup and status on this machine - which library this project archives into, the author email, pages waiting to be pushed, and the last push result. Use when the user asks whether archiving works, why a page is missing from the library (書庫), or runs /artifact-vault:setup.
---

# Setup

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_status.py" --data "${CLAUDE_PLUGIN_DATA}" --author "${user_config.author_email}"
```

Then tell the user, in their language:

1. Whether this project archives into a library, and which one.
2. Anything listed under "To fix", with the exact step.
3. Whether you have an Artifact tool in this session. If you do not, publishing needs a Pro, Max, Team, or Enterprise plan and a session signed in with `/login`; the library itself can still be read on its website.

Do not change files, clone, or push from this skill. It only reports.
