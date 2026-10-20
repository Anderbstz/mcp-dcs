# mcp-docs — Model Context Protocol server for reading and updating Google Docs.
#
# Run it with uv (dependencies live in pyproject.toml):
#
#     uv run server.py
#
# The MCP host (opencode, Claude Desktop, ...) launches this as a subprocess and
# speaks JSON-RPC over stdio. To wire it into opencode, see the `mcp` block in
# the orchestrator's notes / the run_command in the task report.
#
# ---------------------------------------------------------------------------
# CRITICAL stdio rule: stdout carries ONLY JSON-RPC protocol messages. Never
# print() to stdout for logging or debugging — that corrupts the protocol.
# All diagnostics must go to stderr (see the _log helper in auth.py).
# ---------------------------------------------------------------------------
#
# Adding HTTP later (optional): the SDK also supports
#     mcp.run(transport="streamable-http")
# which would serve on a network port instead of stdio. Not used here.

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from mcp.server import MCPServer

import auth
import docs_client

mcp = MCPServer(
    "mcp-docs",
    version="0.1.0",
    instructions=(
        "Read and update Google Docs. Tools accept a full docs.google.com URL "
        "or a bare document ID. Authentication is resolved lazily from the "
        "GOOGLE_SERVICE_ACCOUNT / GOOGLE_CLIENT_SECRETS environment variables."
    ),
)

UrlOrId = Annotated[
    str,
    Field(
        description="A full Google Docs URL "
        "(e.g. https://docs.google.com/document/d/<ID>/edit) or a bare document ID."
    ),
]


def _docs_service():
    """Build an authenticated Docs API client.

    Credentials are resolved HERE, inside a tool call — never at import time —
    so the server can start and list tools without any Google credentials.
    """
    credentials = auth.get_credentials()
    return docs_client.build_docs_service(credentials)


@mcp.tool()
def read_doc(url_or_id: UrlOrId) -> str:
    """Read a Google Doc and return its title, heading structure and plain text.

    Requires the document to be accessible to the configured Google account
    (shared with the OAuth user, or with the service account's client_email).
    """
    doc_id = docs_client.extract_doc_id(url_or_id)
    return docs_client.read_document(_docs_service(), doc_id)


@mcp.tool()
def update_doc(
    url_or_id: UrlOrId,
    action: Annotated[
        Literal["replace_text", "insert_text", "append_text", "append_styled_text"],
        Field(description="Which edit to perform. See each parameter's description."),
    ],
    find: Annotated[
        str | None,
        Field(
            description="replace_text only: the exact text to search for. "
            "All occurrences are replaced."
        ),
    ] = None,
    replace: Annotated[
        str,
        Field(
            description="replace_text only: the replacement text. Use an empty "
            "string to delete the matched text."
        ),
    ] = "",
    text: Annotated[
        str | None,
        Field(description="insert_text/append_text only: the text to insert."),
    ] = None,
    index: Annotated[
        int | None,
        Field(
            description="insert_text only: 1-based character index in the "
            "document body where the text is inserted."
        ),
    ] = None,
    match_case: Annotated[
        bool, Field(description="replace_text only: whether the search is case-sensitive.")
    ] = True,
    font_family: Annotated[
        str, Field(description="append_styled_text only: font family, e.g. 'Arial'.")
    ] = "Arial",
    font_size: Annotated[
        float, Field(description="append_styled_text only: font size in points.")
    ] = 11.0,
) -> str:
    """Edit an existing Google Doc (Google Docs batchUpdate).

    Supported actions:
      * replace_text  — replace every occurrence of `find` with `replace`.
      * insert_text   — insert `text` at the 1-based `index`.
      * append_text   — append `text` at the end of the document.
      * append_styled_text — append `text` at the end with `font_family`/`font_size`.

    Returns a short human-readable confirmation. Every change is a single
    well-scoped batchUpdate; there is no "run arbitrary request" escape hatch.
    """
    doc_id = docs_client.extract_doc_id(url_or_id)
    service = _docs_service()

    if action == "replace_text":
        if not find:
            raise ValueError("action='replace_text' requires a non-empty `find`.")
        docs_client.replace_all_text(service, doc_id, find, replace, match_case)
        return f"Replaced all occurrences of {find!r} in document {doc_id}."

    if action == "insert_text":
        if not text:
            raise ValueError("action='insert_text' requires `text`.")
        if index is None:
            raise ValueError("action='insert_text' requires `index` (>= 1).")
        docs_client.insert_text(service, doc_id, text, index)
        return f"Inserted text at index {index} in document {doc_id}."

    if action == "append_text":
        if not text:
            raise ValueError("action='append_text' requires `text`.")
        docs_client.append_text(service, doc_id, text)
        return f"Appended text to the end of document {doc_id}."

    if action == "append_styled_text":
        if not text:
            raise ValueError("action='append_styled_text' requires `text`.")
        docs_client.append_styled_text(service, doc_id, text, font_family, font_size)
        return (
            f"Appended text in {font_family} {font_size}pt to the end of "
            f"document {doc_id}."
        )

    # Unreachable: `action` is a Literal validated by the SDK.
    raise ValueError(f"Unknown action: {action!r}")


@mcp.tool()
def apply_document_format(
    url_or_id: UrlOrId,
    profile: Annotated[
        str,
        Field(
            description="Formatting profile to apply. Currently only 'formato-ander'."
        ),
    ] = "formato-ander",
) -> str:
    """Apply a formatting profile to a Google Doc and return a short summary.

    The `formato-ander` profile derives heading, caption, note and base-text
    styles from the reference report, then classifies this document's
    paragraphs (headings from the TOC/source outline, figure/table captions,
    notes and conservative lists) before applying styles in batched
    `batchUpdate` calls. The source reference document is only read.
    """
    if profile != "formato-ander":
        raise ValueError(f"Unknown formatting profile: {profile!r}")

    doc_id = docs_client.extract_doc_id(url_or_id)
    result = docs_client.apply_formato_ander(_docs_service(), doc_id)
    classification = result["classification"]
    after = result["after"]
    headings = after["headings"]
    return (
        f"Applied '{profile}' to document {doc_id}.\n"
        f"Headings: H1={headings['HEADING_1']}, H2={headings['HEADING_2']}, "
        f"H3={headings['HEADING_3']}, H4={headings['HEADING_4']}, "
        f"H5={headings['HEADING_5']}, H6={headings['HEADING_6']}.\n"
        f"Captions={classification.get('CAPTION', 0)}, "
        f"notes={classification.get('NOTE', 0)}, "
        f"list items={result['bullets_converted']}, "
        f"paragraphs={after['paragraphs']}."
    )


@mcp.tool()
def create_doc(
    title: Annotated[str, Field(description="Title for the new Google Doc.")],
) -> str:
    """Create a new, empty Google Doc and return its document ID and URL."""
    result = docs_client.create_document(_docs_service(), title)
    doc_id = result.get("documentId")
    return f"Created document '{title}'.\nID: {doc_id}\nURL: https://docs.google.com/document/d/{doc_id}/edit"


if __name__ == "__main__":
    # stdio is the default transport: JSON-RPC in on stdin, out on stdout.
    # Never print to stdout here — stdout is reserved for the protocol; logs go
    # to stderr. HTTP could be enabled later via mcp.run(transport="streamable-http").
    mcp.run()
