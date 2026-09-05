"""Sanitize Tavily MCP tool arguments before remote execution."""

import logging
from typing import Any

logger = logging.getLogger("tavily-interceptor")


# Tavily search API expects lowercase full country names, not ISO codes.
TAVILY_COUNTRY_ALIASES: dict[str, str] = {
    "kr": "south korea",
    "kor": "south korea",
    "korea": "south korea",
    "south korea": "south korea",
    "republic of korea": "south korea",
    "한국": "south korea",
    "대한민국": "south korea",
    "us": "united states",
    "usa": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "gb": "united kingdom",
    "jp": "japan",
    "cn": "china",
    "de": "germany",
    "fr": "france",
}


def normalize_tavily_country(country: Any) -> str | None:
    if country is None:
        return None
    raw = str(country).strip()
    if not raw:
        return None
    key = raw.lower()
    return TAVILY_COUNTRY_ALIASES.get(key, key)


def sanitize_tavily_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if not tool_name.startswith("tavily_"):
        return args

    sanitized = dict(args)
    if "country" not in sanitized:
        return sanitized

    normalized = normalize_tavily_country(sanitized.get("country"))
    if normalized:
        if normalized != sanitized.get("country"):
            logger.info(
                "tavily %s: normalized country %r -> %r",
                tool_name,
                sanitized.get("country"),
                normalized,
            )
        sanitized["country"] = normalized
    else:
        logger.info("tavily %s: removed empty country parameter", tool_name)
        sanitized.pop("country", None)

    return sanitized


# Back-compat: wrapping moved to tool_interceptor.wrap_mcp_tools.
def wrap_tavily_mcp_tools(tools: list) -> list:
    from tool_interceptor import wrap_mcp_tools

    return wrap_mcp_tools(tools)


class TavilyToolCallInterceptor:
    """Deprecated: use tool_interceptor.wrap_mcp_tools after MCPAdapter.list_tools()."""

    def wrap_tools(self, tools: list) -> list:
        return wrap_tavily_mcp_tools(tools)
