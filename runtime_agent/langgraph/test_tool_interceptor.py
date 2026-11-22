"""Unit tests for MCP tool argument interceptors."""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

import tool_interceptor as ti


class _Args(BaseModel):
    query: str = Field(default="q")
    country: str | None = Field(default=None)


def _make_tavily_tool() -> StructuredTool:
    def _run(query: str = "q", country: str | None = None) -> str:
        return f"{query}:{country}"

    return StructuredTool.from_function(
        func=_run,
        name="tavily_web_search",
        description="test",
        args_schema=_Args,
    )


def test_wrap_mcp_tools_patches_structured_tool_without_raising():
    tool = _make_tavily_tool()
    out = ti.wrap_mcp_tools([tool])
    assert len(out) == 1
    assert out[0].name == "tavily_web_search"
    # Must remain callable after Pydantic-safe patch.
    assert callable(out[0].invoke)
    assert callable(out[0].ainvoke)


def test_wrap_invokes_tavily_country_sanitizer():
    tool = _make_tavily_tool()
    wrapped = ti.wrap_mcp_tools([tool])[0]
    result = wrapped.invoke({"query": "anthropic ipo", "country": "KR"})
    assert result == "anthropic ipo:south korea"
