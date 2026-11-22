"""Unit tests for ToolMessage size caps (LLM context protection)."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import langgraph_agent as lg


def test_truncate_under_limit_unchanged():
    text = "hello " * 100
    assert lg.truncate_tool_result_text(text) == text


def test_truncate_over_per_result_max():
    text = "x" * 50_000
    out = lg.truncate_tool_result_text(text, max_chars=1_000)
    assert len(out) <= 1_000
    assert "truncated tool result" in out
    assert "50000" in out


def test_truncate_respects_budget_remaining():
    text = "y" * 10_000
    out = lg.truncate_tool_result_text(
        text, max_chars=32_000, budget_remaining=500
    )
    assert len(out) <= 500
    assert "truncated tool result" in out


def test_cap_tool_messages_per_result_and_turn_budget():
    msgs = [
        HumanMessage(content="q"),
        ToolMessage(content="a" * 40_000, tool_call_id="t1", name="get_raw_text"),
        ToolMessage(content="b" * 40_000, tool_call_id="t2", name="get_raw_text"),
        ToolMessage(content="c" * 40_000, tool_call_id="t3", name="get_raw_text"),
        ToolMessage(content="d" * 40_000, tool_call_id="t4", name="get_raw_text"),
        AIMessage(content="ok"),
    ]
    capped = lg.cap_tool_messages(
        msgs, per_result_max=32_000, turn_max=80_000
    )
    assert isinstance(capped[0], HumanMessage)
    assert isinstance(capped[-1], AIMessage)

    tool_msgs = [m for m in capped if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 4
    assert all(len(m.content) <= 32_000 for m in tool_msgs)
    # First tools consume the turn budget; later ones are exhausted notices only.
    assert sum(len(m.content) for m in tool_msgs[:2]) <= 80_000
    assert any("truncated tool result" in m.content for m in tool_msgs)
    assert any("budget exhausted" in m.content for m in tool_msgs)


def test_cap_tool_node_result_dict():
    result = {
        "messages": [
            ToolMessage(content="z" * 50_000, tool_call_id="tid"),
        ],
        "artifacts": [],
    }
    out = lg._cap_tool_node_result(result)
    assert len(out["messages"][0].content) <= lg.TOOL_RESULT_MAX_CHARS
    assert "truncated tool result" in out["messages"][0].content
    assert out["artifacts"] == []


def test_list_content_normalized():
    msg = ToolMessage(
        content=[{"type": "text", "text": "p" * 40_000}],
        tool_call_id="tid",
    )
    capped = lg.cap_tool_messages([msg], per_result_max=1_000, turn_max=5_000)
    assert isinstance(capped[0].content, str)
    assert len(capped[0].content) <= 1_000
