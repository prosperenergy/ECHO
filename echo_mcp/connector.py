"""Zoom API connector using user OAuth tokens (PKCE flow).

No org-level secrets on the user's machine. The connector uses tokens
obtained via the OAuth Authorization Code + PKCE flow, stored in
~/.echo/tokens.json.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

from .auth import load_tokens, tokens_valid, refresh_access_token
from .registry import resolve_client_id, RegistryError

load_dotenv()

BASE_URL = "https://api.zoom.us/v2"


class NotConfiguredError(Exception):
    """Raised when ECHO is not yet configured or authenticated."""


def _encode_meeting_id(meeting_id: str) -> str:
    """URL-encode a meeting ID/UUID for use in a path segment.

    Zoom requires UUIDs that begin with "/" or contain "//" to be
    double URL-encoded.
    """
    if meeting_id.startswith("/") or "//" in meeting_id:
        return quote(quote(meeting_id, safe=""), safe="")
    return quote(meeting_id, safe="")


class ZoomConnector:
    """Handles Zoom API requests using user OAuth tokens."""

    def __init__(self) -> None:
        self._client_id: str | None = None
        self._tokens: dict | None = None

    @property
    def client_id(self) -> str:
        """Lazily resolve the Client ID from env or registry."""
        if self._client_id is None:
            try:
                self._client_id = resolve_client_id()
            except RegistryError as e:
                raise NotConfiguredError(str(e))
        return self._client_id

    def _load_or_fail(self) -> dict:
        """Load tokens, refreshing if needed."""
        # Trigger client_id resolution (may raise NotConfiguredError)
        _ = self.client_id

        if self._tokens and tokens_valid(self._tokens):
            return self._tokens

        tokens = load_tokens()
        if tokens is None:
            raise NotConfiguredError(
                "Not authenticated yet. Run `echo-login` in your terminal,\n"
                "or ask ECHO to authenticate on your next tool call."
            )
        self._tokens = tokens
        return tokens

    async def _ensure_valid_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        tokens = self._load_or_fail()

        if not tokens_valid(tokens):
            tokens = await refresh_access_token(self.client_id, tokens)
            self._tokens = tokens

        return tokens["access_token"]

    async def _request(
        self, method: str, path: str, params: dict | None = None
    ) -> dict:
        """Make an authenticated request to the Zoom API."""
        token = await self._ensure_valid_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method,
                f"{BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def list_recordings(
        self, from_date: str, to_date: str, page_size: int = 30
    ) -> dict:
        """List cloud recordings for the authenticated user.

        Uses /users/me/ which resolves to whoever owns the OAuth token.

        Args:
            from_date: Start date (YYYY-MM-DD). Max range is 1 month.
            to_date: End date (YYYY-MM-DD).
            page_size: Number of results per page (max 300).
        """
        return await self._request(
            "GET",
            "/users/me/recordings",
            params={"from": from_date, "to": to_date, "page_size": page_size},
        )

    async def get_meeting_recordings(self, meeting_id: str) -> dict:
        """Get recording files (including transcript) for a specific meeting."""
        return await self._request("GET", f"/meetings/{meeting_id}/recordings")

    async def list_past_meetings(self, page_size: int = 30) -> dict:
        """List past meetings hosted by the authenticated user.

        Unlike list_recordings, this includes meetings that were never
        cloud-recorded — e.g. ones that only used AI Companion notes.
        """
        return await self._request(
            "GET",
            "/users/me/meetings",
            params={"type": "previous_meetings", "page_size": page_size},
        )

    async def get_meeting_summary(self, meeting_id: str) -> dict | None:
        """Get the AI Companion meeting summary for a hosted meeting.

        Returns None if the meeting has no AI Companion summary. Zoom only
        exposes summaries to the meeting host at user scope, so meetings
        the user merely attended will also come back as None (404).
        """
        path = f"/meetings/{_encode_meeting_id(meeting_id)}/meeting_summary"
        try:
            return await self._request("GET", path)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_transcript_content(self, download_url: str) -> str:
        """Download the VTT transcript content from a recording download URL."""
        token = await self._ensure_valid_token()
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.text
