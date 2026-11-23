"""MCP Apps host helpers: cache ui:// HTML for AgentCore runtime tool results."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mcp_apps_host")

# tool_name -> app descriptor
_APP_BY_TOOL: dict[str, dict[str, Any]] = {}


def clear() -> None:
    _APP_BY_TOOL.clear()


# MCP servers whose `ui://` resources the web host reads itself (GET /api/mcp-apps/resource),
# like a real MCP Apps host does with resources/read. For these the tool result carries
# only the resource URI, not the HTML, so a 300KB UI is not repeated in every message.
HOST_FETCHED_SERVERS = frozenset(
    {"weather", "notion-remote", "canva", "use-vault", "excalidraw"}
)


def register_tool_app(
    tool_name: str,
    *,
    resource_uri: str,
    html: str | None = None,
    mcp_server: str | None = None,
    csp: dict[str, Any] | None = None,
    mime_type: str | None = None,
) -> None:
    if not tool_name or not resource_uri:
        return
    if not html and (mcp_server or "") not in HOST_FETCHED_SERVERS:
        return  # nothing the host could render
    _APP_BY_TOOL[tool_name] = {
        "resourceUri": resource_uri,
        "html": html,
        "mcpServer": mcp_server,
        "csp": csp or {},
        "mimeType": mime_type or "text/html;profile=mcp-app",
        "tool": tool_name,
    }
    logger.info(
        "Registered MCP App UI for tool=%s uri=%s html_len=%s",
        tool_name,
        resource_uri,
        len(html or ""),
    )


def get_tool_app(tool_name: str | None) -> dict[str, Any] | None:
    if not tool_name:
        return None
    return _APP_BY_TOOL.get(tool_name)


def _fallback_weather_app(tool_name: str) -> dict[str, Any] | None:
    """Last-resort UI for weather tools when Apps cache was not warmed."""
    if tool_name not in {
        "get_weather",
        "get_short_term_forecast",
        "get_metropolitan_weather",
    }:
        return None
    try:
        from mcp_server_weather import VIEW_URI, WEATHER_HTML
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


# use-vault MCP App viewer switches on structuredContent.kind.
_VAULT_TOOL_KIND = {
    "vault_search": "search",
    "vault_read": "note",
    "vault_list": "list",
    "vault_tree": "tree",
    "vault_graph": "graph",
    "vault_backlinks": "backlinks",
}


def _merge_tool_input_fields(
    out: dict[str, Any], tool_input: dict[str, Any] | None
) -> dict[str, Any]:
    """Fill path/title/query from the tool call args when the result body omitted them."""
    if not isinstance(tool_input, dict):
        return out
    path = tool_input.get("path")
    if isinstance(path, str) and path.strip() and not out.get("path"):
        cleaned = path.strip()
        out["path"] = cleaned
        if not out.get("title"):
            out["title"] = cleaned.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    query = tool_input.get("query")
    if isinstance(query, str) and query.strip() and not out.get("query"):
        out["query"] = query.strip()
    prefix = tool_input.get("prefix")
    if isinstance(prefix, str) and not out.get("prefix") and "prefix" in tool_input:
        out["prefix"] = prefix
    return out


def extract_structured_content(
    artifact: Any,
    tool_result_text: str,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> Any:
    """Build UI ``structuredContent`` from ToolMessage artifact and/or text.

    Remote MCP over AgentCore sometimes omits ``ToolMessage.artifact``. The vault
    viewer then parses content JSON; if that JSON is truncated it has no ``kind``
    and shows an empty state. Recover kind from the tool name when needed, and
    slim large note bodies so mcpApp fits in the SSE/srcdoc path.
    """
    import json
    import re

    structured: Any = None
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content") or artifact.get(
            "structuredContent"
        )

    parsed: dict[str, Any] | None = None
    if isinstance(structured, dict):
        parsed = dict(structured)
    elif isinstance(tool_result_text, str):
        stripped = tool_result_text.strip()
        if stripped.startswith("{"):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                parsed = value
            else:
                # Truncated JSON: salvage path / title / a short content preview.
                salvaged: dict[str, Any] = {}
                for key in ("path", "title", "query", "prefix"):
                    m = re.search(
                        rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', stripped
                    )
                    if m:
                        try:
                            salvaged[key] = json.loads(f'"{m.group(1)}"')
                        except json.JSONDecodeError:
                            salvaged[key] = m.group(1)
                m = re.search(r'"content"\s*:\s*"((?:\\.|[^"\\]){0,800})', stripped)
                if m:
                    try:
                        salvaged["content"] = json.loads(f'"{m.group(1)}"')
                    except json.JSONDecodeError:
                        salvaged["content"] = m.group(1)
                if salvaged:
                    parsed = salvaged

    kind = None
    if isinstance(parsed, dict):
        kind = parsed.get("kind")
    if not kind:
        kind = _VAULT_TOOL_KIND.get((tool_name or "").strip())

    if not isinstance(parsed, dict):
        out = {"ok": True, "kind": kind} if kind else structured
        if isinstance(out, dict):
            return _merge_tool_input_fields(out, tool_input)
        return out

    if parsed.get("ok") is False:
        return parsed

    out = {**parsed, "ok": parsed.get("ok", True)}
    if kind:
        out["kind"] = kind
    out.pop("content_html", None)
    if out.get("kind") == "note" and isinstance(out.get("content"), str):
        content = out["content"]
        if len(content) > 2000:
            out["content"] = content[:2000]
    return _merge_tool_input_fields(out, tool_input)


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

    # Always try to produce a kind-bearing structuredContent for vault tools.
    structured_content = extract_structured_content(
        {"structured_content": structured_content}
        if structured_content is not None
        else None,
        tool_result_text,
        tool_name,
        tool_input,
    )
    # Prefer structuredContent for the iframe. Do not also embed the full model
    # text (vault_read notes can be 50KB+); duplicating it in SSE/srcdoc makes
    # the vault viewer fall back to an empty state.
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
    if payload.get("mcpServer") in HOST_FETCHED_SERVERS:
        # The web host reads the ui:// resource itself (and applies its _meta.ui.csp).
        payload.pop("html", None)
        payload.pop("csp", None)
    if isinstance(tool_input, dict):
        payload["toolInput"] = tool_input
    return payload


def extract_ui_resource_uri(tool_metadata: dict[str, Any] | None) -> str | None:
    """Read `_meta.ui.resourceUri` from LangChain MCP tool metadata."""
    if not isinstance(tool_metadata, dict):
        return None
    mcp = tool_metadata.get("mcp")
    if not isinstance(mcp, dict):
        return None
    tool_meta = mcp.get("tool")
    if not isinstance(tool_meta, dict):
        return None
    meta = tool_meta.get("_meta")
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui")
    if isinstance(ui, dict):
        uri = ui.get("resourceUri")
        if isinstance(uri, str) and uri.startswith("ui://"):
            return uri
    legacy = meta.get("ui/resourceUri")
    if isinstance(legacy, str) and legacy.startswith("ui://"):
        return legacy
    return None


def is_app_only_tool(tool_metadata: dict[str, Any] | None) -> bool:
    """True when a tool is meant for the MCP App iframe only (``_meta.ui.visibility``
    omits ``"model"``). Such tools are served through the host proxy and are hidden
    from the model so it does not call UI plumbing (e.g. telemetry) directly."""
    if not isinstance(tool_metadata, dict):
        return False
    mcp = tool_metadata.get("mcp")
    tool_meta = mcp.get("tool") if isinstance(mcp, dict) else None
    meta = tool_meta.get("_meta") if isinstance(tool_meta, dict) else None
    ui = meta.get("ui") if isinstance(meta, dict) else None
    visibility = ui.get("visibility") if isinstance(ui, dict) else None
    return isinstance(visibility, list) and bool(visibility) and "model" not in visibility


def extract_mcp_server_name(tool_metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(tool_metadata, dict):
        return None
    mcp = tool_metadata.get("mcp")
    if not isinstance(mcp, dict):
        return None
    server = mcp.get("server")
    if isinstance(server, dict):
        name = server.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _resource_text_and_meta(contents: Any) -> tuple[str | None, dict[str, Any]]:
    items = contents if isinstance(contents, list) else [contents]
    for item in items:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        meta = getattr(item, "meta", None)
        if meta is None and isinstance(item, dict):
            meta = item.get("meta")
        mime = getattr(item, "mimeType", None) or getattr(item, "mime_type", None)
        if mime is None and isinstance(item, dict):
            mime = item.get("mimeType") or item.get("mime_type")
        csp = None
        if isinstance(meta, dict):
            ui = meta.get("ui")
            if isinstance(ui, dict):
                csp = ui.get("csp")
        return text, {"csp": csp or {}, "mimeType": mime}
    return None, {}


async def cache_apps_from_adapter(
    adapter: Any,
    tools: list[Any],
    *,
    fallback_server_name: str | None = None,
) -> int:
    """Read ui:// resources for tools that declare MCP Apps metadata."""
    registered = 0
    client = getattr(adapter, "_client", None)
    if client is None or not hasattr(client, "read_resource"):
        logger.warning("MCP adapter has no resource client; skipping Apps cache")
        return 0

    for tool in tools:
        metadata = getattr(tool, "metadata", None) or {}
        uri = extract_ui_resource_uri(metadata)
        if not uri:
            continue
        if fallback_server_name in HOST_FETCHED_SERVERS:
            # The web host reads this resource itself; no need to pull the HTML on every
            # agent start. Only the tool -> resource mapping is needed here.
            register_tool_app(
                tool.name,
                resource_uri=uri,
                mcp_server=fallback_server_name,
            )
            registered += 1
            continue
        try:
            contents = await client.read_resource(uri)
            html, extra = _resource_text_and_meta(contents)
            if not html:
                logger.warning("Empty UI resource for %s (%s)", tool.name, uri)
                continue
            register_tool_app(
                tool.name,
                resource_uri=uri,
                html=html,
                # The host proxy routes by the MCP *config key* ("canva", "notion-remote",
                # "weather"). The server's self-declared name ("Canva MCP Server") is only
                # a fallback: it is not unique or stable and the proxy would not know it.
                mcp_server=fallback_server_name or extract_mcp_server_name(metadata),
                csp=extra.get("csp"),
                mime_type=extra.get("mimeType"),
            )
            registered += 1
        except Exception as exc:
            logger.error("Failed to cache MCP App UI for %s: %s", tool.name, exc)
    return registered


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
