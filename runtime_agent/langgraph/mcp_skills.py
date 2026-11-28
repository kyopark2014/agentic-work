"""Discover and load Skills over MCP (SEP-2640) for the LangGraph runtime.

When a connected MCP server advertises ``io.modelcontextprotocol/skills``,
list its skills and eagerly read each ``SKILL.md`` so ``get_skill_instructions``
can serve the body without another round-trip.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from mcp_types import PaginatedRequestParams, Request
from pydantic import TypeAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mcp-skills")

SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"


class ListSkillsRequest(Request[PaginatedRequestParams, Literal["skills/list"]]):
    method: Literal["skills/list"] = "skills/list"
    params: PaginatedRequestParams | None = None


_SkillsListAdapter = TypeAdapter(dict[str, Any])


def server_supports_skills(capabilities: Any) -> bool:
    if capabilities is None:
        return False
    extensions = getattr(capabilities, "extensions", None) or {}
    if isinstance(extensions, dict):
        return SKILLS_EXTENSION_ID in extensions
    return False


@asynccontextmanager
async def _open_mcp_session(connection: dict[str, Any]) -> AsyncIterator[Any]:
    """Open a short-lived ClientSession from langchain-style connection params."""
    import mcp
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    transport = (connection.get("transport") or "").lower()
    if transport in ("http", "streamable_http", "streamable-http"):
        url = connection.get("url")
        if not url:
            raise ValueError("HTTP MCP connection missing url")
        headers = dict(connection.get("headers") or {})
        auth = connection.get("auth")
        http = create_mcp_http_client(headers=headers or None, auth=auth, timeout=None)
        async with http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with mcp.ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
        return

    if transport == "stdio":
        from mcp import StdioServerParameters

        params = StdioServerParameters(
            command=connection.get("command") or "",
            args=list(connection.get("args") or []),
            env=dict(connection.get("env") or {}) or None,
        )
        async with stdio_client(params) as (read, write):
            async with mcp.ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    raise ValueError(f"Unsupported MCP transport for skills: {transport!r}")


async def _list_skills(session: Any) -> list[dict[str, Any]]:
    result = await session.send_request(
        ListSkillsRequest(params=PaginatedRequestParams()),
        _SkillsListAdapter,
    )
    if not isinstance(result, dict):
        return []
    skills = result.get("skills") or []
    return [s for s in skills if isinstance(s, dict)]


async def _read_skill_md(session: Any, uri: str) -> str:
    result = await session.read_resource(uri)
    contents = getattr(result, "contents", None) or []
    for item in contents:
        text = getattr(item, "text", None)
        if text:
            return str(text)
        # Some SDKs use model_dump
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            if dumped.get("text"):
                return str(dumped["text"])
    raise ValueError(f"No text content for skill resource {uri}")


def _body_from_skill_md(raw: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter and markdown body (same rules as SkillManager)."""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        import yaml

        frontmatter = yaml.safe_load(parts[1]) or {}
    except Exception:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return frontmatter, parts[2].strip()


async def discover_mcp_skills(server_params: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return skill dicts ready for ``skill.register_mcp_skills``.

    Each item: name, description, instructions, uri, server, origin.
    """
    discovered: list[dict[str, Any]] = []
    for server_name, connection in (server_params or {}).items():
        try:
            async with _open_mcp_session(connection) as session:
                caps = session.server_capabilities
                if not server_supports_skills(caps):
                    logger.info("MCP server %s has no skills extension; skip", server_name)
                    continue
                entries = await _list_skills(session)
                logger.info(
                    "MCP server %s skills/list → %s skill(s)",
                    server_name,
                    len(entries),
                )
                for entry in entries:
                    uri = (entry.get("uri") or "").strip()
                    if not uri:
                        continue
                    fm = entry.get("frontmatter") or {}
                    if not isinstance(fm, dict):
                        fm = {}
                    name = (fm.get("name") or "").strip()
                    description = str(fm.get("description") or "")
                    raw = await _read_skill_md(session, uri)
                    parsed_fm, body = _body_from_skill_md(raw)
                    if not name:
                        name = str(parsed_fm.get("name") or "").strip()
                    if not description:
                        description = str(parsed_fm.get("description") or "")
                    if not name:
                        # skill://name/SKILL.md
                        try:
                            name = uri.split("://", 1)[1].split("/", 1)[0]
                        except Exception:
                            name = f"{server_name}-skill"
                    discovered.append(
                        {
                            "name": name,
                            "description": description,
                            "instructions": body,
                            "uri": uri,
                            "server": server_name,
                            "origin": f"mcp:{server_name}",
                            "path": f"mcp://{server_name}/{name}",
                        }
                    )
                    logger.info(
                        "Loaded MCP skill %s from %s (%s bytes)",
                        name,
                        server_name,
                        len(body),
                    )
        except Exception as exc:
            logger.warning("MCP skills discovery failed for %s: %s", server_name, exc)
            if getattr(exc, "__cause__", None):
                logger.warning("  cause: %s", exc.__cause__)
    return discovered
