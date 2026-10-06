---
name: page
description: Use whenever you create, edit, or publish an HTML page or an Artifact, including when the user says a page must not be archived ("不要進書庫", "don't archive this"). Published pages are archived automatically in projects with a library (vault), so a page the user wants kept out must be marked by this skill. It also applies the library's template and category. Run its prepare step first; if it prints NO_VAULT, ignore this skill and continue normally.
---

# Page

Pages published in a project with a vault are archived automatically after you publish them. This skill makes those pages consistent: the library's template, its stylesheet and logo, and a category the library can sort by.

## 1. Prepare

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_page.py" --data "${CLAUDE_PLUGIN_DATA}" prepare
```

- `NO_VAULT`: stop using this skill. Build and publish the page the way you normally would.
- Otherwise it prints JSON with `types` (each has `label`, `template`, `use_diagram`) and `diagram_tools`.

## 2. Choose the type

- Pick the type that matches the request (for example a plan → `dev-plan`). Use the user's words; ask only when two types fit equally well.
- If no type fits, build the page without a template. Do not add a `vault:type` meta tag; the library files it as unsorted.

## 3. Start from the template

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/vault_page.py" --data "${CLAUDE_PLUGIN_DATA}" render <type> <path/to/page.html>
```

This writes the template with the stylesheet and logo already inlined. Then edit that file:

- Keep every section and its order. Replace all sample text with the real content. Keep the table of contents in sync with the section ids.
- Keep `<meta name="vault:type" content="<type>">` and any script the template ships (for example the table-of-contents highlight).
- Do not restyle the template. Write content, not CSS.

## 4. Diagrams

Follow `use_diagram` for the type:

- `archify`: use the archify skill to build the diagram as `diagrams/<name>.html` next to the page, keep the template's `<iframe src="diagrams/<name>.html">`, and publish the diagram as a supporting file (the Artifact tool's `files` parameter).
- `mermaid`: replace the iframe with `<pre class="mermaid">…</pre>` inside the same container.
- `null`: remove the diagram section's iframe and describe the structure in a short list instead.

## 5. Keep a page out of the library

Skipping this skill does not keep a page out: the capture hook archives every published page in the project. If the user says the page must not be archived (for example "這頁不要進書庫", "don't archive this"), add this tag near the top of the page before publishing:

```
<meta name="vault:skip">
```

The capture hook skips any page that contains `vault:skip`, anywhere in the file. So never write that token in a page's content (for example when the page itself discusses archiving); write "skip marker" instead.

## 6. Publish

Publish with the Artifact tool as usual, including `files` for supporting files. The capture hook reports "queued" (已排入書庫) afterwards; tell the user that the page is archived, or that it was skipped.
