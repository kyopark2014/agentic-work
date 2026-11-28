"""Discover and load Skills over MCP (SEP-2640) for the LangGraph runtime.

SEP-2640 host rules this module follows:

- Listing/connection MUST NOT pre-fetch skill file bytes (progressive disclosure).
- Before use, verify ``resources/read`` bytes against the retained manifest
  (size + SHA-256 digest) and compare ``SKILL.md`` frontmatter field-by-field.
- Skill identity is ``(server, uri)``; names alone are not unique.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
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


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _manifest_entry_for_uri(resources: Any, uri: str) -> dict[str, Any] | None:
    if resources == "dynamic":
        return None
    if not isinstance(resources, list):
        return None
    for item in resources:
        if isinstance(item, dict) and (item.get("uri") or "").strip() == uri:
            return item
    return None


def parse_skill_md(raw: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter and markdown body."""
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


def _frontmatter_equal(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Field-by-field compare; both sides coerced to plain JSON-ish values."""
    exp_keys = set(expected.keys())
    act_keys = set(actual.keys())
    if exp_keys != act_keys:
        return False
    for key in exp_keys:
        if expected.get(key) != actual.get(key):
            return False
    return True


async def _read_resource_bytes(session: Any, uri: str) -> bytes:
    result = await session.read_resource(uri)
    contents = getattr(result, "contents", None) or []
    for item in contents:
        text = getattr(item, "text", None)
        if text is not None:
            return str(text).encode("utf-8")
        blob = getattr(item, "blob", None)
        if blob:
            import base64

            if isinstance(blob, str):
                return base64.b64decode(blob)
            return bytes(blob)
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            if dumped.get("text") is not None:
                return str(dumped["text"]).encode("utf-8")
            if dumped.get("blob"):
                import base64

                return base64.b64decode(dumped["blob"])
    raise ValueError(f"No content for skill resource {uri}")


def verify_skill_md_bytes(
    raw: bytes,
    *,
    uri: str,
    resources: Any,
    expected_frontmatter: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Verify size/digest against retained manifest and frontmatter identity.

    Raises ValueError on mismatch (host MUST NOT use failed content).
    """
    if resources == "dynamic":
        text = raw.decode("utf-8")
        fm, body = parse_skill_md(text)
        if not _frontmatter_equal(expected_frontmatter or {}, fm):
            raise ValueError(f"frontmatter mismatch for dynamic skill {uri}")
        return fm, body

    manifest = _manifest_entry_for_uri(resources, uri)
    if manifest is None:
        raise ValueError(f"URI {uri} is not in the retained skill manifest")

    expected_size = manifest.get("size")
    expected_digest = (manifest.get("digest") or "").strip()
    if expected_size is not None and int(expected_size) != len(raw):
        raise ValueError(
            f"size mismatch for {uri}: expected {expected_size}, got {len(raw)}"
        )
    actual_digest = _sha256_digest(raw)
    if expected_digest and actual_digest != expected_digest:
        raise ValueError(
            f"digest mismatch for {uri}: expected {expected_digest}, got {actual_digest}"
        )

    text = raw.decode("utf-8")
    fm, body = parse_skill_md(text)
    if not _frontmatter_equal(expected_frontmatter or {}, fm):
        raise ValueError(f"frontmatter mismatch for {uri}")
    return fm, body


async def load_verified_skill_md(
    connection: dict[str, Any],
    *,
    uri: str,
    resources: Any,
    expected_frontmatter: dict[str, Any],
) -> str:
    """``resources/read`` SKILL.md and verify against the retained entry."""
    async with _open_mcp_session(connection) as session:
        raw = await _read_resource_bytes(session, uri)
        _fm, body = verify_skill_md_bytes(
            raw,
            uri=uri,
            resources=resources,
            expected_frontmatter=expected_frontmatter,
        )
        return body


def load_verified_skill_md_sync(
    connection: dict[str, Any],
    *,
    uri: str,
    resources: Any,
    expected_frontmatter: dict[str, Any],
) -> str:
    """Sync wrapper safe to call from a LangChain ``@tool`` (may already be in a loop)."""

    async def _run() -> str:
        return await load_verified_skill_md(
            connection,
            uri=uri,
            resources=resources,
            expected_frontmatter=expected_frontmatter,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_run())).result(timeout=120)


async def discover_mcp_skills(server_params: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """List skill metadata only (no ``resources/read``).

    Each item retains ``connection``, ``frontmatter``, and ``resources`` (manifest)
    so ``get_skill_instructions`` can fetch and verify on demand.
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
                    "MCP server %s skills/list → %s skill(s) (metadata only)",
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
                    if not name:
                        try:
                            name = uri.split("://", 1)[1].split("/", 1)[0]
                        except Exception:
                            name = f"{server_name}-skill"
                    discovered.append(
                        {
                            "name": name,
                            "description": description,
                            # Body left empty until get_skill_instructions (no pre-fetch).
                            "instructions": "",
                            "uri": uri,
                            "server": server_name,
                            "origin": f"mcp:{server_name}",
                            "path": f"mcp://{server_name}/{name}",
                            "connection": connection,
                            "frontmatter": dict(fm),
                            "resources": entry.get("resources"),
                        }
                    )
                    logger.info(
                        "Discovered MCP skill %s from %s (uri=%s, deferred body)",
                        name,
                        server_name,
                        uri,
                    )
        except Exception as exc:
            logger.warning("MCP skills discovery failed for %s: %s", server_name, exc)
            if getattr(exc, "__cause__", None):
                logger.warning("  cause: %s", exc.__cause__)
    return discovered
