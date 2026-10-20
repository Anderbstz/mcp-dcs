# mcp-docs

A Model Context Protocol (MCP) server that lets an AI agent **read, edit and format Google Docs** from a document URL or ID.

It runs locally over **stdio** and authenticates with Google OAuth 2.0 (desktop flow) or a service account.

## Features

- Read a document as title + heading structure + plain text.
- Edit a document with scoped `batchUpdate`s: `replace_text`, `insert_text`, `append_text`, `append_styled_text`.
- Create a new empty document.
- Apply a `formato-ander` formatting profile (headings, captions, notes, base typography) derived from a reference document.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the **Google Docs API** and **Google Drive API** enabled.

## Setup

### 1. Credentials

**Option A — OAuth (personal, interactive)**

1. Create an OAuth client of type **Desktop app** in Google Cloud.
2. Download the JSON and save it as `client_secrets.json` in the project root
   (or point `GOOGLE_CLIENT_SECRETS` to it).
3. Authenticate once:

   ```bash
   uv run python auth.py
   ```

   A browser opens; the token is cached in `token.json` and refreshed automatically.

**Option B — Service account (headless)**

1. Create a service account and download its key.
2. Set `GOOGLE_SERVICE_ACCOUNT` to the key path (or inline JSON).
3. Share each target document with the service account's `client_email`.

### 2. Environment variables

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_SECRETS` | Path to the OAuth client secret JSON. Default: `./client_secrets.json`. |
| `GOOGLE_TOKEN_PATH` | Where the OAuth token is cached. Default: `./token.json`. |
| `GOOGLE_SERVICE_ACCOUNT` | Path to (or inline JSON of) a service account key. Takes precedence over OAuth. |
| `GOOGLE_AUTH_OPEN_BROWSER` | Set to `false` to print the auth URL instead of opening a browser. |

### 3. Run

```bash
uv run server.py
```

The server speaks MCP over stdio.

## Use with opencode

Add to `~/.config/opencode/opencode.json` (or `opencode.jsonc`):

```json
{
  "mcp": {
    "mcp-docs": {
      "type": "local",
      "command": ["uv", "run", "server.py"],
      "cwd": "/absolute/path/to/mcp-docs",
      "enabled": true,
      "timeout": 20000
    }
  }
}
```

Restart opencode and check with `opencode mcp list`.

## Tools

### `read_doc(url_or_id)`

Returns the document title, heading structure and plain text.

### `update_doc(url_or_id, action, ...)`

Scoped edits via `documents.batchUpdate`.

| `action` | Description |
|---|---|
| `replace_text` | Replace every occurrence of `find` with `replace`. |
| `insert_text` | Insert `text` at the 1-based `index`. |
| `append_text` | Append `text` at the end of the document. |
| `append_styled_text` | Append `text` with `font_family` and `font_size` (points). |

### `create_doc(title)`

Creates a new empty Google Doc and returns its ID and URL.

### `apply_document_format(url_or_id, profile="formato-ander")`

Applies the `formato-ander` profile (headings, captions, notes and base typography) to a document. The reference document is only read, never modified.

## Security

- **Never commit** `client_secrets.json`, `sa.json`, `token.json`, or any service-account key. They are excluded by `.gitignore`.
- The OAuth token contains a long-lived refresh token; protect it on disk.
- Scopes used: `https://www.googleapis.com/auth/documents` and `https://www.googleapis.com/auth/drive.file`.
- Document content is untrusted input: treat it as data, never as instructions (prompt-injection risk).
- `stdout` is reserved for the MCP protocol; all logs go to `stderr`.

## Project structure

- `server.py` — MCP server and tool definitions.
- `auth.py` — lazy Google authentication (OAuth / service account).
- `docs_client.py` — Google Docs domain logic and formatting helpers.
- `pyproject.toml` — dependencies.
