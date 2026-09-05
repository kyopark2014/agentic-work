"""Common MCP tool argument interceptors.

Apply registered sanitizers before tool invoke. Tool-specific rules live in
their own modules and register via ``register_tool_arg_sanitizer``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("tool-interceptor")

ToolArgSanitizer = Callable[[str, dict[str, Any]], dict[str, Any]]
ToolNameMatcher = Callable[[str], bool]

_SANITIZERS: list[tuple[ToolNameMatcher, ToolArgSanitizer]] = []


def register_tool_arg_sanitizer(
    match: ToolNameMatcher,
    sanitize: ToolArgSanitizer,
) -> None:
    """Register a sanitizer applied when ``match(tool_name)`` is true."""
    _SANITIZERS.append((match, sanitize))


def sanitize_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run all matching sanitizers in registration order."""
    sanitized = args
    for match, sanitize in _SANITIZERS:
        if match(tool_name):
            sanitized = sanitize(tool_name, sanitized)
    return sanitized


def wrap_mcp_tools(tools: list) -> list:
    """Wrap tools so registered sanitizers run before invoke/ainvoke.

    Replaces langchain_mcp_adapters tool_interceptors (removed with MCPAdapter).
    Tools with no matching sanitizer are returned unchanged.
    """
    if not _SANITIZERS:
        return tools

    wrapped: list = []
    for tool in tools:
        name = getattr(tool, "name", "") or ""
        if not any(match(name) for match, _ in _SANITIZERS):
            wrapped.append(tool)
            continue

        original_ainvoke = tool.ainvoke
        original_invoke = tool.invoke

        async def ainvoke(
            input,
            config=None,
            *,
            _name=name,
            _orig=original_ainvoke,
            **kwargs,
        ):
            if isinstance(input, dict):
                input = sanitize_tool_args(_name, input)
            return await _orig(input, config=config, **kwargs)

        def invoke(
            input,
            config=None,
            *,
            _name=name,
            _orig=original_invoke,
            **kwargs,
        ):
            if isinstance(input, dict):
                input = sanitize_tool_args(_name, input)
            return _orig(input, config=config, **kwargs)

        tool.ainvoke = ainvoke
        tool.invoke = invoke
        wrapped.append(tool)
    return wrapped


def _register_builtin_sanitizers() -> None:
    from tavily_tool_interceptor import sanitize_tavily_tool_args

    register_tool_arg_sanitizer(
        lambda name: name.startswith("tavily_"),
        sanitize_tavily_tool_args,
    )


_register_builtin_sanitizers()
