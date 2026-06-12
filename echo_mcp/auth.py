"""OAuth 2.0 Authorization Code + PKCE flow for Zoom.

Architecture:
- IT creates a "General App" (OAuth) in Zoom Marketplace
- IT shares only the Client ID (public, safe to distribute)
- User runs `echo-login` and authorizes with their own Zoom account
- Tokens are stored locally in ~/.echo/tokens.json
- Refresh tokens are used to stay authenticated without re-login

No client secret ever touches the user's machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Event
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

ZOOM_AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
REDIRECT_PORT = 8090
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_DIR = Path.home() / ".echo"
TOKEN_FILE = TOKEN_DIR / "tokens.json"

# Scopes: read own recordings, AI Companion summaries for hosted meetings,
# past-meeting listing + basic user info (granular scope names)
SCOPES = (
    "cloud_recording:read:content "
    "cloud_recording:read:list_user_recordings "
    "meeting:read:summary "
    "meeting:read:list_meetings "
    "user:read:user"
)


def _bff_url() -> str | None:
    """The BFF base URL, if one is configured. If set, token exchange goes
    through the BFF (which holds the Zoom client_secret) instead of Zoom
    directly. Setting this avoids having to distribute the secret to users.
    """
    url = os.environ.get("ECHO_BFF_URL", "").rstrip("/")
    return url or None


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _save_tokens(token_data: dict) -> None:
    """Save tokens to disk."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_data["saved_at"] = time.time()
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    TOKEN_FILE.chmod(0o600)  # Only owner can read


def load_tokens() -> dict | None:
    """Load saved tokens from disk, or None if not found."""
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def tokens_valid(tokens: dict) -> bool:
    """Check if the access token is still valid (with 60s buffer)."""
    saved_at = tokens.get("saved_at", 0)
    expires_in = tokens.get("expires_in", 0)
    return time.time() < saved_at + expires_in - 60


class AuthServiceUnreachable(Exception):
    """Raised when the BFF is unreachable (likely VPN issue)."""


async def refresh_access_token(client_id: str, tokens: dict) -> dict:
    """Use the refresh token to get a new access token.

    Zoom requires client_secret even with PKCE, so we forward the refresh
    through the BFF (which holds the secret). If no BFF is configured,
    attempt a direct call (will only succeed if ZOOM_CLIENT_SECRET is set
    in the environment, which is not recommended).
    """
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh token available. Please run: echo-login")

    bff = _bff_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if bff:
                resp = await client.post(
                    f"{bff}/refresh",
                    json={
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                    },
                )
            else:
                client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
                resp = await client.post(
                    ZOOM_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            resp.raise_for_status()
            new_tokens = resp.json()
            _save_tokens(new_tokens)
            return new_tokens
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
        if bff:
            raise AuthServiceUnreachable(
                f"Cannot reach the ECHO auth service at {bff}. "
                f"Are you connected to the Percona VPN? "
                f"ECHO needs VPN access briefly (~once per hour) to refresh your "
                f"Zoom session. Once refreshed, you can go back off-VPN until the "
                f"next hour.\n\n(underlying error: {e})"
            )
        raise


def login(client_id: str) -> dict:
    """Run the full OAuth PKCE login flow.

    1. Opens browser to Zoom authorization page
    2. Starts local HTTP server to catch the redirect
    3. Exchanges auth code for tokens (with PKCE verifier)
    4. Saves tokens to ~/.echo/tokens.json
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    auth_params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    auth_url = f"{ZOOM_AUTHORIZE_URL}?{auth_params}"

    # Capture the auth code via local HTTP server
    auth_code: dict = {}
    done = Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)

            if query.get("state", [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"State mismatch. Please try again.")
                return

            if "error" in query:
                self.send_response(400)
                self.end_headers()
                error_msg = query.get("error_description", query["error"])[0]
                self.wfile.write(f"Authorization failed: {error_msg}".encode())
                auth_code["error"] = error_msg
                done.set()
                return

            auth_code["code"] = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            # Do NOT claim success yet. The token exchange still has to succeed.
            # The terminal is the source of truth.
            self.wfile.write(
                b"<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
                b"<h1>Authorization received</h1>"
                b"<p>ECHO is now exchanging the authorization code for an access token.</p>"
                b"<p><strong>Check your terminal</strong> for the final result, "
                b"then you can close this tab.</p>"
                b"</body></html>"
            )
            done.set()

        def log_message(self, format, *args):
            pass  # Suppress HTTP log noise

    server = HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    server.timeout = 120  # 2 minute timeout for user to authorize

    print(f"\nOpening Zoom authorization in your browser...\n")
    print(f"If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for the callback
    while not done.is_set():
        server.handle_request()

    server.server_close()

    if "error" in auth_code:
        raise RuntimeError(f"Authorization failed: {auth_code['error']}")

    if "code" not in auth_code:
        raise RuntimeError("No authorization code received. Timed out?")

    # Exchange auth code for tokens
    import httpx as httpx_sync  # Sync for the CLI context

    bff = _bff_url()
    if bff:
        print(f"Exchanging authorization code via BFF ({bff})...")
    else:
        print("Exchanging authorization code directly with Zoom...")

    with httpx_sync.Client(timeout=15) as client:
        if bff:
            resp = client.post(
                f"{bff}/exchange",
                json={
                    "client_id": client_id,
                    "code": auth_code["code"],
                    "code_verifier": verifier,
                    "redirect_uri": REDIRECT_URI,
                },
            )
        else:
            # Direct path requires ZOOM_CLIENT_SECRET in env
            client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
            resp = client.post(
                ZOOM_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code["code"],
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            # Surface the real reason the exchange failed
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise RuntimeError(
                f"Token exchange failed ({resp.status_code}): {body}\n"
                f"Common causes:\n"
                f"  - Scopes in ECHO don't match what the Zoom app was granted\n"
                f"  - Client ID is incorrect or the app was deactivated\n"
                f"  - Redirect URI doesn't match (must be exactly "
                f"{REDIRECT_URI})\n"
                f"  - BFF is unreachable (check VPN, or ECHO_BFF_URL)"
            )
        tokens = resp.json()

    _save_tokens(tokens)
    print("\n[OK] Authenticated! Tokens saved to ~/.echo/tokens.json")
    print("     You can now use ECHO tools from your AI client.")
    return tokens


def logout() -> None:
    """Remove stored tokens."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("Logged out. Tokens removed.")
    else:
        print("No tokens found — already logged out.")
