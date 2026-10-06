---
name: tag
description: Change the category (type) of a page already in the artifact library (書庫), for example "把這頁分類成研究計畫" or "mark that page as dev-plan", or list what the library holds. Use when the user wants to re-categorize an archived page or runs /artifact-vault:tag.
---

# Tag

Archived versions are never edited. A category change is stored as an override in the library and applied by the library site.

1. Find the page. If the user gave an artifact link or id, use it. Otherwise list the library and match by title:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_tag.py" --data "${CLAUDE_PLUGIN_DATA}" list
   ```

   Each line is `id  type  versions  title`. Ask the user if more than one title matches.

2. Set the type:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_tag.py" --data "${CLAUDE_PLUGIN_DATA}" --author "${user_config.author_email}" set <id or link> <type>
   ```

   The type must be one of the library's types or `unsorted`; an unknown type prints the allowed list. A page still waiting to be pushed cannot be tagged yet; say so and suggest trying again after the next turn.

3. Tell the user the result line the script printed.
