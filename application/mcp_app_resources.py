"""`ui://` resources for MCP Apps, read by the web host (GET /api/mcp-apps/resource).

A real MCP Apps host reads a UI resource once with ``resources/read`` and caches it. The
tool result then only references the resource, so a large UI is not repeated in every
message, and the resource's ``_meta.ui`` (CSP, prefersBorder) is known before the frame is
created.

Sources, per server:
  weather        built into this app (application/mcp_server_weather.py)
  notion-remote  viewer shipped with this app (application/mcp_app_views/)
  canva          read from mcp.canva.com with the user's OAuth token
  use-vault      read from the use-vault MCP runtime (SigV4, ECS task role)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from application import mcp_apps_host

logger = logging.getLogger("mcp_app_resources")

CACHE_TTL_SECONDS = 600
VIEWS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_app_views")

MIME = "text/html;profile=mcp-app"

NOTION_VIEWER_URI = "ui://agentic-work/notion-viewer.html"

# Remote servers: only these URI prefixes may be read through the host.
_REMOTE_URI_PREFIX = {"canva": "ui://canva/", "use-vault": "ui://vault/"}

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _local_weather(uri: str) -> dict[str, Any]:
    from application.mcp_server_weather import VIEW_URI, WEATHER_HTML

    if uri != VIEW_URI:
        raise ValueError(f"Unknown weather resource: {uri}")
    return {
        "html": WEATHER_HTML,
        "mimeType": MIME,
        "meta": {"csp": {"resourceDomains": ["https://www.weather.go.kr"]}},
    }


def _local_notion(uri: str) -> dict[str, Any]:
    if uri != NOTION_VIEWER_URI:
        raise ValueError(f"Unknown notion-remote resource: {uri}")
    path = os.path.join(VIEWS_DIR, "dist", "notion-viewer.html")
    if not os.path.isfile(path):
        raise ValueError("Notion viewer is not built (run application/mcp_app_views/build.py)")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    # No external origins: the viewer is self-contained and talks only to the host.
    return {"html": html, "mimeType": MIME, "meta": {"csp": {}, "prefersBorder": False}}


_LOCAL = {"weather": _local_weather, "notion-remote": _local_notion}


async def _read_remote(server: str, uri: str, user_id: str | None) -> dict[str, Any]:
    prefix = _REMOTE_URI_PREFIX[server]
    if not uri.startswith(prefix):
        raise ValueError(f"{server} resource not allowed: {uri}")
    async with mcp_apps_host.remote_session(server, user_id) as session:
        result = await session.read_resource(uri)
    content = result.contents[0]
    text = getattr(content, "text", None)
    if not isinstance(text, str) or not text:
        raise ValueError(f"{server} resource has no HTML: {uri}")
    dumped = content.model_dump(by_alias=True, exclude_none=True, mode="json")
    ui = ((dumped.get("_meta") or {}).get("ui")) or {}
    meta: dict[str, Any] = {"csp": ui.get("csp") or {}}
    if isinstance(ui.get("prefersBorder"), bool):
        meta["prefersBorder"] = ui["prefersBorder"]
    return {"html": text, "mimeType": dumped.get("mimeType") or MIME, "meta": meta}


async def read(server_name: str, uri: str, user_id: str | None) -> dict[str, Any]:
    """Return ``{html, mimeType, meta:{csp, prefersBorder}}`` for a UI resource."""
    server = mcp_apps_host.normalize_server(server_name)
    uri = (uri or "").strip()
    if not uri.startswith("ui://"):
        raise ValueError("Only ui:// resources can be read")

    key = (server, uri)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    if server in _LOCAL:
        resource = _LOCAL[server](uri)
    elif server in _REMOTE_URI_PREFIX:
        resource = await _read_remote(server, uri, user_id)
    else:
        raise ValueError(f"No UI resources are served for server: {server_name!r}")

    _cache[key] = (time.time(), resource)
    return resource
