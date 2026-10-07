---
name: backfill
description: Add existing claude.ai artifacts to the project's artifact library (書庫) — pages published before the vault was set up, published from another folder, or whose newest version is missing. Use for "把這頁補進書庫", "backfill <link>", "把搜尋框相關的頁面都補進書庫", or /artifact-vault:backfill <link or keyword>.
---

# Backfill

Pages normally enter the library when they are published. This skill adds pages that were published earlier: Claude reads each page back from claude.ai with the Artifact tool, and the script queues it exactly like a publish (same vault:skip and credential checks).

Only the person's own artifacts can be read back in full. For a page owned by someone else, the Artifact tool returns a summary, not the files: tell the user that page must be backfilled by its owner.

## One page: `backfill <link>`

1. Read the page: Artifact `action: "read"` with the link. Note the version in the result header (for example `version 1791299089-6c62`) and the page `<title>`. Stop here if the header does not say the user owns it.
2. List its files: Artifact `action: "list"`, `scope: "files"`, same link.
3. Read every listed file into a new empty folder in your scratchpad directory (not the project): Artifact `action: "read"`, `paths: [...all listed paths...]`, `out_dir: <that folder>`. `index.html` is the page itself.
4. Queue it:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_backfill.py" --data "${CLAUDE_PLUGIN_DATA}" --author "${user_config.author_email}" add --url <link> --version <version> --title "<title>" --files <folder>
   ```

   Add `--capabilities db,…` when the read header lists runtime capabilities. The script prints `queued`, `skipped` (already in the library, or the page has vault:skip) or `refused` (looks like it holds a credential; say so, never repeat the match).

## Many pages: `backfill <keyword>`

1. List the user's artifacts: Artifact `action: "list"`, `limit: 50` (page with more calls if needed). Keep the ones whose title matches the keyword.
2. See which are already archived:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_backfill.py" --data "${CLAUDE_PLUGIN_DATA}" check <link> <link> …
   ```

   Each line is `id  missing|archived  count  versions`.
3. Show the user the list (title, link, missing or archived) and **ask which ones to backfill before reading any page**. An archived page can still be backfilled when its newest version is missing; the script skips versions it already has.
4. For each confirmed page, do the one-page steps 1–4.

## After queuing

1. Push now, so the pages can be tagged in this turn:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/push.py" --data "${CLAUDE_PLUGIN_DATA}"
   ```

   Then check the result with `/artifact-vault:setup` if anything looks wrong.
2. Backfilled pages usually have no type (`unsorted`). Suggest a type for each from its content, confirm with the user, and set it with the tag skill (`vault_tag.py … set <id> <type>`).
3. Report what was queued, skipped, or refused, by title.
