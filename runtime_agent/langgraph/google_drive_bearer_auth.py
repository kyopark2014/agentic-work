"""httpx2 Auth that injects the current Google Drive access token per request."""

from __future__ import annotations

import os

import httpx2


class GoogleDriveBearerAuth(httpx2.Auth):
    """Read token from chat module / env on every request so refresh works without rebuild."""

    def auth_flow(self, request: httpx2.Request):
        token = ""
        try:
            import chat

            token = (getattr(chat, "google_drive_access_token", None) or "").strip()
        except Exception:
            token = ""
        if not token:
            token = (os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN") or "").strip()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request
