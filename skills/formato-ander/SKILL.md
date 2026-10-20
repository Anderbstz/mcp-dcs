---
name: formato-ander
description: "Trigger: formato-ander, informe, APA, dar formato a Google Doc, mcp-docs. Apply the formato-ander report style to a Google Doc via the mcp-docs MCP."
license: Apache-2.0
metadata:
  author: "anderbstz"
  version: "1.0"
---

## Activation Contract

Use when the user asks to format a Google Doc as "formato-ander", an academic or corporate report, or explicitly asks to use the `mcp-docs` MCP.

## Hard Rules

- Use the `mcp-docs` MCP tools. Do not edit the document by other means.
- Typography: **Times New Roman 12** everywhere. Body justified, line spacing 1.5.
- Headings: H1–H4 **bold**; H3 **bold + italic**.
- Chapter numbers are **typed text** (`CAPÍTULO N. TITLE`), never a list: Google Docs cannot auto-number headings.
- Subsection numbers are **typed text** (`N.M.`, `N.M.K.`). The Docs API cannot set list start numbers or custom prefixes (`glyphFormat` / `startNumber` are read-only; only `bulletPreset` is writable).
- Use real lists only where the source has list markers. Conclusions and Recommendations use **bullets**. Numbered lists use `NUMBERED_DECIMAL_ALPHA_ROMAN`; nesting is set with leading tabs.
- Do not add a cover page or table of contents unless the user asks.
- Never modify the reference/source document.

## Decision Gates

| Situation | Action |
|---|---|
| Flat text dump | Rebuild content: strip markers, merge wrapped lines, classify headings/lists |
| Chapter numbering | Type `CAPÍTULO N.` into the heading; do not use a list |
| Needs `2.1` subsections | Type `N.M.` as text; list prefixes are not settable |
| Conclusions / Recommendations | Bullet list |

## Execution Steps

1. `read_doc(url)` and inspect structure (headings, lists, markers).
2. Optionally `apply_document_format(url, "formato-ander")`.
3. Set Times New Roman 12 over the body; bold headings; H3 italic.
4. Remove manual markers and type chapter/section numbers.
5. Convert list markers to real lists (bullets or numbered).
6. Verify headings count, list items, font and bold; report gaps honestly.

## Output Contract

Return the doc URL, counts (headings, list items), the formatting applied, and known gaps (no real tables, no automatic numbering).

## References

- `docs_client.py` — `apply_formato_ander`, list and text-style helpers.
