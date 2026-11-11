"""MCP Apps host helpers for ECS: weather UI fallback + iframe tool proxy."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mcp_apps_host")

_APP_BY_TOOL: dict[str, dict[str, Any]] = {}


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


def build_app_payload(tool_name: str | None, tool_result_text: str) -> dict[str, Any] | None:
    app = get_tool_app(tool_name) or (
        _fallback_weather_app(tool_name) if tool_name else None
    )
    if not app:
        return None
    return {
        **app,
        "toolResult": {
            "content": [{"type": "text", "text": tool_result_text}],
        },
    }


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


async def call_mcp_tool(
    mcp_server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call weather tools in-process on ECS (iframe refresh).

    AgentCore runs the full MCP stdio servers; the web container only needs
    weather for Apps iframe reloads, so we invoke the Python helpers directly.
    """
    arguments = arguments or {}
    server = (mcp_server or "").strip().lower()
    name = (tool_name or "").strip()

    if server != "weather":
        raise ValueError(
            f"ECS mcp-apps proxy only supports weather (got mcpServer={mcp_server!r})"
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
