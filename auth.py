"""Google authentication for mcp-docs.

Nothing here runs at import time: credentials are resolved lazily, the first
time a tool actually needs them. That keeps an unauthenticated smoke test
(``tools/list``) working even with no credentials on disk.

Two supported modes, selected by environment variables:

1. Service account (headless, no browser):
     GOOGLE_SERVICE_ACCOUNT=/path/to/service-account.json
   Best for servers / CI. Share the target Doc with the service account's
   ``client_email``, otherwise the API returns 403.

2. OAuth 2.0 installed-app (desktop) flow, with a cached, auto-refreshed token:
     GOOGLE_CLIENT_SECRETS=/path/to/client_secrets.json   (default: ./client_secrets.json)
     GOOGLE_TOKEN_PATH=/path/to/token.json                (default: ./token.json)
   On first use a browser window opens; afterwards the refresh token in
   ``GOOGLE_TOKEN_PATH`` is reused silently.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Full read/write access to Docs, plus access to files this app creates/opens
# via Drive (drive.file). We deliberately avoid drive.readonly / drive (full).
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

_HERE = Path(__file__).resolve().parent
DEFAULT_CLIENT_SECRETS = _HERE / "client_secrets.json"
DEFAULT_TOKEN_PATH = _HERE / "token.json"

# Module-level cache so repeated tool calls do not re-authenticate.
_credentials_cache: Credentials | None = None


def _log(message: str) -> None:
    """Log to stderr only. stdout is reserved for the JSON-RPC protocol."""
    print(f"[mcp-docs] {message}", file=sys.stderr)


def _service_account_credentials() -> Credentials | None:
    """Return service-account credentials if GOOGLE_SERVICE_ACCOUNT is configured."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if not raw:
        return None

    # Accept either a path to a JSON key or the JSON content itself.
    if raw.lstrip().startswith("{"):
        info = json.loads(raw)
        _log("Using service-account credentials from GOOGLE_SERVICE_ACCOUNT (inline JSON)")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"GOOGLE_SERVICE_ACCOUNT points to {path}, but that file does not exist."
        )
    _log(f"Using service-account credentials from {path}")
    return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)


def _oauth_credentials() -> Credentials:
    """Run the installed-app flow, reusing/refreshing a cached token when possible."""
    client_secrets = Path(
        os.environ.get("GOOGLE_CLIENT_SECRETS", str(DEFAULT_CLIENT_SECRETS))
    ).expanduser()
    token_path = Path(
        os.environ.get("GOOGLE_TOKEN_PATH", str(DEFAULT_TOKEN_PATH))
    ).expanduser()

    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not client_secrets.is_file():
            raise FileNotFoundError(
                "No usable Google credentials found. Provide either:\n"
                f"  - GOOGLE_SERVICE_ACCOUNT=<service-account.json>, or\n"
                f"  - GOOGLE_CLIENT_SECRETS=<client_secrets.json> (looked at {client_secrets})."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
        # port=0 picks a free localhost port for the redirect. Set
        # GOOGLE_AUTH_OPEN_BROWSER=false if this process cannot open a browser.
        open_browser = os.environ.get("GOOGLE_AUTH_OPEN_BROWSER", "true").lower() != "false"
        # The OAuth library prints the authorization URL to stdout. When this
        # runs inside the MCP server, stdout is reserved for JSON-RPC, so we
        # redirect it to stderr to avoid corrupting the protocol. A timeout
        # stops the flow from hanging forever if the callback never arrives.
        with contextlib.redirect_stdout(sys.stderr):
            creds = flow.run_local_server(
                port=0,
                open_browser=open_browser,
                timeout_seconds=600,
                prompt="select_account",
            )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    _log(f"Cached OAuth token at {token_path}")
    return creds


def get_credentials() -> Credentials:
    """Resolve Google credentials once and cache them for the process.

    Raises a clear error if neither auth mode is configured.
    """
    global _credentials_cache
    if _credentials_cache is not None and _credentials_cache.valid:
        return _credentials_cache

    creds = _service_account_credentials()
    if creds is None:
        creds = _oauth_credentials()

    _credentials_cache = creds
    return creds


if __name__ == "__main__":
    # One-time (or post-expiry) interactive authorization. Run with:
    #     uv run python auth.py
    get_credentials()
    _log("Authentication complete.")
