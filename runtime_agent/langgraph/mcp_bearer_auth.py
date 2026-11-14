"""httpx2 Auth that injects a per-user OAuth access token for a named remote MCP server.

Tokens arrive in the invoke payload (``mcp_oauth_tokens``) and are kept in
``chat.mcp_oauth_tokens``; they are read on every request so a refreshed token
applies without rebuilding the agent.
"""

from __future__ import annotations

import os

import httpx2


def env_name(name: str) -> str:
    return "MCP_OAUTH_TOKEN_" + "".join(c if c.isalnum() else "_" for c in name).upper()


class McpBearerAuth(httpx2.Auth):
    def __init__(self, name: str):
        self.name = name

    def auth_flow(self, request: httpx2.Request):
        token = ""
        try:
            import chat

            token = ((getattr(chat, "mcp_oauth_tokens", None) or {}).get(self.name) or "").strip()
        except Exception:
            token = ""
        if not token:
            token = (os.environ.get(env_name(self.name)) or "").strip()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request
