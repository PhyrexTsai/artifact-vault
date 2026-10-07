---
name: purge
description: Permanently remove a page from the artifact library (書庫) and from all of the library's git history — for a page that must not be kept, such as one with a leaked secret or personal data. Use for "把這頁從書庫徹底刪掉", "purge <link>", or /artifact-vault:purge <link or id>. Not for hiding a page or changing its category.
---

# Purge

A purge rewrites the library repo's history and force-pushes it. It cannot be undone, and it changes every commit after the page was first added, so treat it as a last resort.

1. Show what will be removed (read only):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_purge.py" --data "${CLAUDE_PLUGIN_DATA}" plan <id or link>
   ```

   Tell the user the title, how many commits and files, and versions waiting on this machine. Stop only when `files` is 0, `commits` is empty and `queued` is empty: the page is not in the library and not waiting to be pushed. If only `queued` has versions, still purge: that drops them and stops any machine from pushing the page later.

2. **Ask the user to confirm by typing the id.** Explain first: history is rewritten, every clone of the library repo must reset afterwards, and the page stays wherever it was already copied (see step 4). Do not continue on a vague "yes".

3. Run it with the id the user typed:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_purge.py" --data "${CLAUDE_PLUGIN_DATA}" --author "${user_config.author_email}" run <id> --confirm <id the user typed>
   ```

   If it says the vault changed while purging, run it again. Report any other error as it is.

4. Tell the user what is left to do (the README lists the same):
   - **Library site**: redeploy it (automatic when the host deploys on push; otherwise deploy by hand). The page then returns 404.
   - **Other clones of the library repo**: run `git fetch origin && git reset --hard origin/<branch>`. A plain `git pull` would merge the old history back. Clones made by this plugin on other machines follow the rewrite by themselves, once they run a plugin version that has the purge skill.
   - **GitHub**: copies outside the branch (pull request refs, forks, cached views) are only removed by GitHub Support.
   - **A leaked secret**: rotate it. Removing the page does not make the secret safe again.
   - The page cannot be backfilled or pushed again while `purged/<id>.json` is in the library.
