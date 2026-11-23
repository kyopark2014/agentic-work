"""MCP Apps host helpers for ECS: weather UI fallback + iframe tool proxy."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("mcp_apps_host")

_APP_BY_TOOL: dict[str, dict[str, Any]] = {}


def resolve_vault_actor_id(user_id: str | None) -> str:
    """Account id for use-vault ``actor_id`` (ob-note vault owner).

    Prefer ``use_vault_actor_id`` when the local login id (e.g. ``admin``) is not
    the same as the Google email that owns the vault on the ob-note server.
    """
    try:
        from application import utils

        cfg = utils.load_config() or {}
    except Exception:
        cfg = {}
    override = (
        (cfg.get("use_vault_actor_id") if isinstance(cfg, dict) else None)
        or os.environ.get("use_vault_actor_id")
        or os.environ.get("USE_VAULT_ACTOR_ID")
        or ""
    ).strip()
    if override:
        return override
    return (user_id or "").strip() or "default"


def clear() -> None:
    _APP_BY_TOOL.clear()


def register_tool_app(
    tool_name: str,
    *,
    resource_uri: str,
    html: str,
    mcp_server: str | None = None,
    csp: dict[str, Any] | None = None,
    mime_type: str | None = None,
) -> None:
    if not tool_name or not resource_uri or not html:
        return
    _APP_BY_TOOL[tool_name] = {
        "resourceUri": resource_uri,
        "html": html,
        "mcpServer": mcp_server,
        "csp": csp or {},
        "mimeType": mime_type or "text/html;profile=mcp-app",
        "tool": tool_name,
    }


def get_tool_app(tool_name: str | None) -> dict[str, Any] | None:
    if not tool_name:
        return None
    return _APP_BY_TOOL.get(tool_name)


def _fallback_weather_app(tool_name: str) -> dict[str, Any] | None:
    if tool_name not in {
        "get_weather",
        "get_short_term_forecast",
        "get_metropolitan_weather",
    }:
        return None
    try:
        from application.mcp_server_weather import VIEW_URI, WEATHER_HTML
    except Exception as exc:
        logger.warning("weather UI fallback import failed: %s", exc)
        return None
    register_tool_app(
        tool_name,
        resource_uri=VIEW_URI,
        html=WEATHER_HTML,
        mcp_server="weather",
        csp={"resourceDomains": ["https://www.weather.go.kr"]},
    )
    return get_tool_app(tool_name)


def build_app_payload(
    tool_name: str | None,
    tool_result_text: str,
    structured_content: Any = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    app = get_tool_app(tool_name) or (
        _fallback_weather_app(tool_name) if tool_name else None
    )
    if not app:
        return None
    import json

    # Mirror runtime_agent/langgraph/mcp_apps_host.py (recover kind when artifact dropped).
    _VAULT_TOOL_KIND = {
        "vault_search": "search",
        "vault_read": "note",
        "vault_list": "list",
        "vault_tree": "tree",
        "vault_graph": "graph",
        "vault_backlinks": "backlinks",
    }

    if not (isinstance(structured_content, dict) and structured_content.get("kind")):
        parsed = structured_content if isinstance(structured_content, dict) else None
        if parsed is None and isinstance(tool_result_text, str):
            stripped = tool_result_text.strip()
            if stripped.startswith("{"):
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict):
                    parsed = value
        kind = (parsed or {}).get("kind") if isinstance(parsed, dict) else None
        if not kind:
            kind = _VAULT_TOOL_KIND.get((tool_name or "").strip())
        if isinstance(parsed, dict):
            structured_content = {**parsed, "ok": parsed.get("ok", True)}
            if kind:
                structured_content["kind"] = kind
            structured_content.pop("content_html", None)
            if (
                structured_content.get("kind") == "note"
                and isinstance(structured_content.get("content"), str)
                and len(structured_content["content"]) > 2000
            ):
                structured_content["content"] = structured_content["content"][:2000]
        elif kind:
            structured_content = {"ok": True, "kind": kind}

    if isinstance(structured_content, dict) and structured_content.get("kind"):
        stub: dict[str, Any] = {"ok": True}
        for key in ("ok", "kind", "path", "query", "prefix"):
            if key in structured_content:
                stub[key] = structured_content[key]
        tool_result: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(stub, ensure_ascii=False)}],
            "structuredContent": structured_content,
        }
    elif structured_content is not None:
        tool_result = {
            "content": [{"type": "text", "text": tool_result_text}],
            "structuredContent": structured_content,
        }
    else:
        tool_result = {
            "content": [{"type": "text", "text": tool_result_text}],
        }
    payload: dict[str, Any] = {**app, "toolResult": tool_result}
    if isinstance(tool_input, dict):
        payload["toolInput"] = tool_input
    return payload


def normalize_tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        import json

        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                texts.append(str(getattr(block, "text") or ""))
            else:
                texts.append(str(block))
        return "\n".join(t for t in texts if t)
    return str(content)


# Tools an MCP App iframe may call on the signed-in user's behalf, per remote MCP.
# The iframe is untrusted page content, so this is an explicit allowlist (never
# "whatever the server lists").
NOTION_IFRAME_TOOLS = frozenset(
    {
        "notion-search",
        "notion-fetch",
        "notion-list-recent-pages",
        "notion-list-favorite-pages",
        "notion-list-private-pages",
        "notion-list-shared-pages",
    }
)

# Canva: the tools its MCP Apps UIs use (app-only helpers, job pollers, thumbnails)
# plus the one explicit user action in the UI (turn a candidate into a design).
CANVA_IFRAME_TOOLS = frozenset(
    {
        "track-event-lifecycle",
        "get-design-candidates",
        "get-design-thumbnails",
        "get-design-thumbnail",
        "get-create-design-async-job",
        "get-generate-image-job",
        "get-separate-image-layers-job",
        "get-assets",
        "create-design-from-candidate",
    }
)

# Names an MCP server declares for itself, as older stored mcpApp payloads carried them.
_SERVER_ALIASES = {"canva mcp server": "canva"}

# use-vault (ob-note): read tools + in-app Save (vault_write). Other mutations stay agent-only.
VAULT_IFRAME_TOOLS = frozenset(
    {
        "vault_tree",
        "vault_list",
        "vault_read",
        "vault_search",
        "vault_graph",
        "vault_backlinks",
        # Viewer-only: resolve relative markdown images to data URLs.
        "vault_asset",
        # Viewer Preview: server-side markdown → HTML (ob-note share pipeline).
        "vault_render",
        # Viewer Save (Preview/Edit/Save toolbar).
        "vault_write",
    }
)

# Excalidraw OSS MCP Apps: widget-only tools (model uses read_me / create_view).
EXCALIDRAW_IFRAME_TOOLS = frozenset(
    {
        "export_to_excalidraw",
        "save_checkpoint",
        "read_checkpoint",
    }
)
EXCALIDRAW_MCP_URL = "https://mcp.excalidraw.com/mcp"

# server name (as used in the mcpApp payload) -> (auth module, allowed tools)
_REMOTE_OAUTH_MCPS: dict[str, tuple[str, frozenset[str]]] = {
    "notion-remote": ("notion_auth", NOTION_IFRAME_TOOLS),
    "canva": ("canva_auth", CANVA_IFRAME_TOOLS),
}
# Servers reached with the ECS task role (SigV4) instead of a per-user OAuth token.
_IAM_MCPS: dict[str, frozenset[str]] = {"use-vault": VAULT_IFRAME_TOOLS}
# Public Streamable HTTP MCPs (no OAuth / SigV4). URL + iframe tool allowlist.
_PUBLIC_HTTP_MCPS: dict[str, tuple[str, frozenset[str]]] = {
    "excalidraw": (EXCALIDRAW_MCP_URL, EXCALIDRAW_IFRAME_TOOLS),
}
_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    **{k: v[1] for k, v in _REMOTE_OAUTH_MCPS.items()},
    **_IAM_MCPS,
    **{k: v[1] for k, v in _PUBLIC_HTTP_MCPS.items()},
}


@asynccontextmanager
async def remote_session(server: str, user_id: str | None):
    """Open an MCP ClientSession to a hosted MCP for iframe resource/tool proxying.

    OAuth servers (Notion/Canva) use the user's own token; IAM servers (use-vault) use
    the ECS task role (SigV4); public servers (excalidraw) need no credentials.
    """
    import asyncio
    import importlib

    if (
        server not in _REMOTE_OAUTH_MCPS
        and server not in _IAM_MCPS
        and server not in _PUBLIC_HTTP_MCPS
    ):
        raise ValueError(f"No remote MCP session for server: {server}")
    if not user_id:
        raise ValueError("Sign in required")

    import mcp
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    if server in _PUBLIC_HTTP_MCPS:
        url, _allowed = _PUBLIC_HTTP_MCPS[server]
        http = create_mcp_http_client(timeout=None)
        async with http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with mcp.ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
        return

    if server in _IAM_MCPS:
        from application import agentcore_mcp

        url, region = await asyncio.to_thread(agentcore_mcp.use_vault_endpoint)
        http = create_mcp_http_client(auth=agentcore_mcp.SigV4Auth(region), timeout=None)
        async with http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with mcp.ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
        return

    module_name, _allowed = _REMOTE_OAUTH_MCPS[server]
    try:
        auth = importlib.import_module(f"application.{module_name}")
    except ImportError:  # pragma: no cover - flat layout
        auth = importlib.import_module(module_name)

    token = await asyncio.to_thread(auth.get_access_token, user_id)
    if not token:
        raise ValueError(f"{server} is not connected. Connect it from the MCP menu.")

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http:
        async with streamable_http_client(auth.PROVIDER.mcp_url, http_client=http) as streams:
            async with mcp.ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def _call_remote_oauth_tool(
    server: str,
    user_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call a hosted MCP (Notion/Canva) with the signed-in user's OAuth token."""
    allowed = _ALLOWED_TOOLS[server]
    if tool_name not in allowed:
        raise ValueError(f"{server} tool not allowed from MCP Apps: {tool_name}")
    if not user_id:
        raise ValueError("Sign in required")
    if server == "use-vault":
        # These tools take the account as an argument. The iframe is untrusted page content,
        # so never use the caller-supplied value: always act as the signed-in / configured owner.
        arguments = {**arguments, "actor_id": resolve_vault_actor_id(user_id)}
    async with remote_session(server, user_id) as session:
        result = await session.call_tool(tool_name, arguments)
    # Keep the whole result (structuredContent, image blocks, _meta): UIs read them.
    return result.model_dump(by_alias=True, exclude_none=True, mode="json")


def normalize_server(name: str | None) -> str:
    """Canonical server key (lower-case, declared-name aliases resolved)."""
    server = (name or "").strip().lower()
    return _SERVER_ALIASES.get(server, server)


async def call_mcp_tool(
    mcp_server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Call weather tools in-process on ECS (iframe refresh).

    AgentCore runs the full MCP stdio servers; the web container only needs
    weather for Apps iframe reloads, so we invoke the Python helpers directly.
    """
    arguments = arguments or {}
    server = normalize_server(mcp_server)
    name = (tool_name or "").strip()

    if server in _ALLOWED_TOOLS:
        return await _call_remote_oauth_tool(server, user_id, name, arguments)

    if server != "weather":
        raise ValueError(
            "ECS mcp-apps proxy only supports weather, notion-remote, canva, "
            f"use-vault and excalidraw (got mcpServer={mcp_server!r})"
        )

    from application.mcp_server_weather import (
        get_metropolitan_weather,
        get_short_term_forecast,
        get_weather,
    )

    if name in {"get_weather", "get_short_term_forecast"}:
        region = str(arguments.get("region") or arguments.get("city") or "전국")
        if name == "get_short_term_forecast":
            text = get_short_term_forecast(region=region)
        else:
            text = get_weather(region=region)
    elif name == "get_metropolitan_weather":
        city = str(arguments.get("city") or "")
        text = get_metropolitan_weather(city=city)
    else:
        raise ValueError(f"Unsupported weather tool: {tool_name}")

    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }
