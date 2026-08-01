"""AgentCore SSE helpers: notification sink updates and stream event processing."""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from application.tool_result_parsers import (
        _format_references_markdown,
        get_tool_info,
    )
except ImportError:
    from tool_result_parsers import (  # type: ignore
        _format_references_markdown,
        get_tool_info,
    )

logger = logging.getLogger("agentcore_sse")

tool_info_list: dict = {}
tool_result_list: list = []
tool_name_list: dict = {}


def add_notification(notification_queue, message):
    if notification_queue is not None:
        notification_queue.notify(message)


def update_streaming_result(notification_queue, message):
    if notification_queue is not None:
        notification_queue.stream(message)


def commit_streaming_segment(notification_queue, message: str):
    if notification_queue is not None:
        notification_queue.commit_text_segment(message)


def tool_slot_update(notification_queue, slot_key: str, message: str):
    if notification_queue is not None:
        notification_queue.tool_update(slot_key, message)


def on_tool_use_started(
    notification_queue,
    current: str,
    tool_use_id: str,
    tool_info_list_local: dict,
) -> str:
    """Commit pre-tool assistant text when a new tool call starts."""
    if tool_use_id not in tool_info_list_local and current:
        commit_streaming_segment(notification_queue, current)
        return ""
    return current


def _format_tool_input(input_value) -> str:
    if isinstance(input_value, (dict, list)):
        try:
            return json.dumps(input_value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(input_value)
    return str(input_value) if input_value is not None else ""


def normalize_bedrock_message_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return "".join(parts)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _collect_tool_result_artifacts(tool_name, tool_result, references: list, image_url: list) -> None:
    content, urls, refs = get_tool_info(tool_name, tool_result)
    if refs:
        references.extend(refs)
        logger.info("refs: %s", refs)
    if urls:
        image_url.extend(urls)
        logger.info("urls: %s", urls)
    if content:
        logger.info("content: %s", content)


def _process_tool_events(data_json: dict, notification_queue, stream_state: dict) -> None:
    """Shared tool / toolResult handling for strands and langgraph SSE shapes."""
    tool_input_cache = stream_state.setdefault("tool_input_cache", {})
    references = stream_state["references"]
    image_url = stream_state["image_url"]

    if "toolResult" in data_json:
        tool_result = data_json["toolResult"]
        tool_use_id = data_json["toolUseId"]
        if data_json.get("tool"):
            tool_name_list[tool_use_id] = data_json["tool"]
        tool_name = tool_name_list.get(tool_use_id, data_json.get("tool", "unknown"))
        logger.info("[tool_result] %s", tool_result)

        effective_input = tool_input_cache.get(tool_use_id, {})
        stream_state["current"] = on_tool_use_started(
            notification_queue, stream_state["current"], tool_use_id, tool_info_list
        )
        tool_slot_update(
            notification_queue,
            f"{tool_use_id}:input",
            f"Tool: {tool_name}, Input: {_format_tool_input(effective_input)}",
        )
        tool_slot_update(
            notification_queue, f"{tool_use_id}:result", f"Tool Result: {str(tool_result)}"
        )
        _collect_tool_result_artifacts(tool_name, tool_result, references, image_url)
        return

    if "tool" in data_json:
        tool = data_json["tool"]
        input_val = data_json.get("input", {})
        tool_use_id = data_json["toolUseId"]
        tool_name_list[tool_use_id] = tool
        if isinstance(input_val, dict) and input_val:
            tool_input_cache[tool_use_id] = input_val
        effective_input = tool_input_cache.get(
            tool_use_id,
            input_val if isinstance(input_val, dict) else {},
        )
        stream_state["current"] = on_tool_use_started(
            notification_queue, stream_state["current"], tool_use_id, tool_info_list
        )
        if effective_input:
            tool_slot_update(
                notification_queue,
                f"{tool_use_id}:input",
                f"Tool: {tool}, Input: {_format_tool_input(effective_input)}",
            )


def _process_strands_sse_event(data_json: dict, notification_queue, stream_state: dict) -> None:
    if "data" in data_json:
        text = normalize_bedrock_message_content(data_json["data"])
        logger.info("[data] %s", text)
        stream_state["current"] += text
        update_streaming_result(notification_queue, stream_state["current"])
        return

    if "result" in data_json:
        final_output = data_json["result"]
        logger.info("[result] %s", final_output)
        stream_state["result"] = final_output.get("messages", [])
        if "image_url" in final_output:
            stream_state["image_url"] = final_output.get("image_url", [])
        return

    _process_tool_events(data_json, notification_queue, stream_state)


def _process_langgraph_sse_event(data_json: dict, notification_queue, stream_state: dict) -> None:
    if "data" in data_json:
        text = normalize_bedrock_message_content(data_json["data"])
        logger.info("[data] %s", text)
        stream_state["current"] += text
        update_streaming_result(notification_queue, stream_state["current"])
        return

    if "result" in data_json:
        final_output = data_json["result"]
        logger.info("[result] %s", final_output)
        messages = final_output.get("messages", [])
        raw_content = messages[-1].get("content") if messages else ""
        stream_state["result"] = normalize_bedrock_message_content(raw_content)
        if "image_url" in final_output:
            stream_state["image_url"] = final_output.get("image_url", [])
        return

    _process_tool_events(data_json, notification_queue, stream_state)


def _process_sse_event(
    data_json: dict,
    notification_queue,
    stream_state: dict,
    agent_type: str = "langgraph",
) -> None:
    if agent_type == "strands":
        _process_strands_sse_event(data_json, notification_queue, stream_state)
    else:
        _process_langgraph_sse_event(data_json, notification_queue, stream_state)


def _finalize_agent_result(result, current, references: list, notification_queue):
    if isinstance(result, list):
        # strands-style messages list
        text_parts = []
        for msg in result:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text_parts.append(normalize_bedrock_message_content(msg.get("content")))
        result = "".join(text_parts) if text_parts else current
    elif not isinstance(result, str):
        result = normalize_bedrock_message_content(result)

    if not result and current:
        result = current

    if references:
        result += _format_references_markdown(references)

    if notification_queue is not None:
        notification_queue.result(result)
    return result
