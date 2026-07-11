import json
import logging
import queue
import re
import threading
from typing import Any, Generator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from application.api.routes_auth import require_user_id
from application import agentcore_client, task_store
from application import chat
from application.notification_queue import QueueNotificationSink

logger = logging.getLogger("routes_chat")

router = APIRouter(prefix="/api/tasks", tags=["chat"])

HISTORY_MODE = "Enable"

_TOOL_INPUT_RE = re.compile(r"^Tool: (.+?), Input: (.+)$", re.DOTALL)
_TOOL_RESULT_RE = re.compile(r"^Tool Result: (.+)$", re.DOTALL)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _upsert_tool_event(tool_events: list[dict[str, Any]], mapped: dict[str, Any]) -> None:
    if mapped["type"] in ("tool", "tool_result"):
        tool_use_id = mapped.get("toolUseId")
        for i, existing in enumerate(tool_events):
            if existing.get("type") == mapped["type"] and existing.get("toolUseId") == tool_use_id:
                tool_events[i] = mapped
                return
    tool_events.append(mapped)


def _track_tool_event(
    tool_events: list[dict[str, Any]],
    tool_meta: dict[str, dict[str, Any]],
    mapped: dict[str, Any],
) -> list[dict[str, Any]]:
    """Persist tool events and backfill tool call when only result arrives."""
    events_to_emit = [mapped]
    tool_use_id = mapped.get("toolUseId")

    if mapped["type"] == "tool":
        tool_meta[tool_use_id] = {
            "tool": mapped.get("tool"),
            "input": mapped.get("input", {}),
        }
        _upsert_tool_event(tool_events, mapped)
        return events_to_emit

    if mapped["type"] == "tool_result" and tool_use_id:
        meta = tool_meta.get(tool_use_id, {})
        has_tool_event = any(
            event.get("type") == "tool" and event.get("toolUseId") == tool_use_id
            for event in tool_events
        )
        if not has_tool_event:
            tool_event = {
                "type": "tool",
                "tool": meta.get("tool", "unknown"),
                "input": meta.get("input", {}),
                "toolUseId": tool_use_id,
            }
            _upsert_tool_event(tool_events, tool_event)
            events_to_emit.insert(0, tool_event)
        if meta.get("tool"):
            mapped = {**mapped, "tool": meta["tool"]}
            events_to_emit[-1] = mapped
        _upsert_tool_event(tool_events, mapped)
        return events_to_emit

    if mapped["type"] in ("tool", "tool_result", "info"):
        _upsert_tool_event(tool_events, mapped)
    return events_to_emit


def _normalize_tool_use_id(tool_use_id: str) -> str:
    if tool_use_id.endswith(":input"):
        return tool_use_id[: -len(":input")]
    if tool_use_id.endswith(":result"):
        return tool_use_id[: -len(":result")]
    return tool_use_id


def _map_sink_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    data = event.get("data", "")

    if event_type == "markdown":
        return {"type": "token", "data": data}

    if event_type == "info":
        tool_match = _TOOL_INPUT_RE.match(str(data))
        if tool_match:
            tool_name = tool_match.group(1)
            raw_input = tool_match.group(2)
            try:
                tool_input = json.loads(raw_input)
            except json.JSONDecodeError:
                tool_input = raw_input
            return {
                "type": "tool",
                "tool": tool_name,
                "input": tool_input,
                "toolUseId": _normalize_tool_use_id(event.get("toolUseId", tool_name)),
            }
        result_match = _TOOL_RESULT_RE.match(str(data))
        if result_match:
            return {
                "type": "tool_result",
                "toolUseId": _normalize_tool_use_id(event.get("toolUseId", "")),
                "data": result_match.group(1),
            }
        return {"type": "info", "data": data}

    return event


def _run_agent_thread(
    *,
    prompt: str,
    user_id: str,
    mcp_servers: list[str],
    model_name: str,
    skill_list: list[str],
    guardrail_enabled: bool,
    runtime_session_id: str,
    message_queue: queue.Queue,
    result_holder: dict[str, Any],
) -> None:
    sink = QueueNotificationSink(message_queue)

    try:
        logger.info("Using AgentCore runtime invoke_agent_runtime")
        response, image_url = agentcore_client.run_agent(
            prompt,
            user_id,
            HISTORY_MODE,
            mcp_servers,
            model_name,
            notification_queue=sink,
            skill_list=skill_list,
            guardrail_enabled=guardrail_enabled,
            runtime_session_id=runtime_session_id,
        )
        if not isinstance(response, str):
            response = json.dumps(response, ensure_ascii=False)
        result_holder["content"] = response
        result_holder["images"] = image_url or []
    except Exception as exc:
        logger.exception("Agent run failed")
        result_holder["error"] = str(exc)
    finally:
        message_queue.put(None)


@router.post("/{task_id}/chat")
def chat_stream(task_id: str, body: ChatRequest, request: Request):
    user_id = require_user_id(request)
    task = task_store.get_task(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    prompt = body.prompt.strip()
    chat.user_id = user_id
    chat.update(task["model_name"])

    task_store.add_message(task_id, "user", prompt)

    message_queue: queue.Queue = queue.Queue()
    result_holder: dict[str, Any] = {"content": "", "images": []}

    worker = threading.Thread(
        target=_run_agent_thread,
        kwargs={
            "prompt": prompt,
            "user_id": user_id,
            "mcp_servers": task["mcp_servers"],
            "model_name": task["model_name"],
            "skill_list": task["skills"],
            "guardrail_enabled": task["guardrail_enabled"],
            "runtime_session_id": task["runtime_session_id"],
            "message_queue": message_queue,
            "result_holder": result_holder,
        },
        daemon=True,
    )
    worker.start()

    def event_generator() -> Generator[str, None, None]:
        tool_events: list[dict[str, Any]] = []
        tool_meta: dict[str, dict[str, Any]] = {}
        streamed_text = ""

        while True:
            try:
                item = message_queue.get(timeout=300)
            except queue.Empty:
                error_text = "Agent timeout"
                task_store.add_message(task_id, "assistant", f"Error: {error_text}")
                yield _sse_event({"type": "error", "data": error_text})
                yield _sse_event(
                    {"type": "done", "content": f"Error: {error_text}", "images": []}
                )
                break

            if item is None:
                break

            mapped = _map_sink_event(item)
            if not mapped:
                continue

            if mapped["type"] == "token":
                streamed_text = mapped["data"]
            elif mapped["type"] in ("tool", "tool_result", "info"):
                for tool_event in _track_tool_event(tool_events, tool_meta, mapped):
                    yield _sse_event(tool_event)
                continue

            yield _sse_event(mapped)

        if "error" in result_holder:
            error_text = f"Error: {result_holder['error']}"
            task_store.add_message(task_id, "assistant", error_text)
            yield _sse_event({"type": "error", "data": result_holder["error"]})
            yield _sse_event({"type": "done", "content": error_text, "images": []})
            return

        final_content = result_holder.get("content") or streamed_text
        images = result_holder.get("images") or []

        task_store.add_message(
            task_id,
            "assistant",
            final_content,
            images=images,
            tool_events=tool_events,
        )

        yield _sse_event(
            {
                "type": "done",
                "content": final_content,
                "images": images,
            }
        )

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
