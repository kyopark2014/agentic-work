"""Chat SSE stream orchestration: sink event mapping, tool timeline, agent worker."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from typing import Any, Generator

from application import chat
from application import task_store
from application.notification_queue import QueueNotificationSink
from application.runtime_mode import run_agent

logger = logging.getLogger("chat_stream_service")

SSE_HEARTBEAT_INTERVAL_SECONDS = 15
# Idle timeout: no agent queue output for this long → treat as stuck.
# (Previously this was a wall-clock limit from stream start.)
AGENT_STREAM_TIMEOUT_SECONDS = 1200
# Absolute safety cap for a single chat turn (4 hours).
AGENT_STREAM_MAX_SECONDS = 14400
# After SSE times out / client disconnects, wait this long for the agent
# worker to finish so the final answer can still be persisted for refresh.
LATE_PERSIST_WAIT_SECONDS = 1800
STREAMING_PREFIX_COMPARISON_LENGTH = 80
CLIENT_SAFE_AGENT_ERROR = "Agent processing failed"
CLIENT_SAFE_AGENT_TIMEOUT = "Agent timeout"

_TOOL_INPUT_RE = re.compile(r"^Tool: (.+?), Input:\s*(.*)$", re.DOTALL)
_TOOL_RESULT_RE = re.compile(r"^Tool Result: (.+)$", re.DOTALL)


def sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_keepalive() -> str:
    return ": keepalive\n\n"


def parse_tool_input(raw_input: str) -> Any:
    stripped = raw_input.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return raw_input


class ChatStreamService:
    """Transforms agent notification-queue events into SSE payloads and timelines."""

    def __init__(
        self,
        *,
        heartbeat_interval: int = SSE_HEARTBEAT_INTERVAL_SECONDS,
        stream_timeout: int = AGENT_STREAM_TIMEOUT_SECONDS,
        stream_max_seconds: int = AGENT_STREAM_MAX_SECONDS,
    ) -> None:
        self.heartbeat_interval = heartbeat_interval
        self.stream_timeout = stream_timeout
        self.stream_max_seconds = stream_max_seconds

    @staticmethod
    def is_segment_reset(previous: str, new: str) -> bool:
        if not previous.strip():
            return False
        if not new:
            return True
        return not new.startswith(previous)

    def handle_token(
        self,
        tool_events: list[dict[str, Any]],
        streamed_text: str,
        new_text: str,
    ) -> tuple[str, dict[str, Any] | None]:
        if new_text == streamed_text:
            return streamed_text, None
        committed = None
        if self.is_segment_reset(streamed_text, new_text):
            committed = self.flush_text_segment(tool_events, streamed_text)
        return new_text, committed

    @staticmethod
    def flush_text_segment(
        timeline: list[dict[str, Any]], text: str
    ) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped:
            return None
        if (
            timeline
            and timeline[-1].get("type") == "text"
            and timeline[-1].get("data", "").strip() == stripped
        ):
            return None
        timeline.append({"type": "text", "data": stripped})
        return {"type": "text", "data": stripped}

    @staticmethod
    def is_streaming_prefix_of_final(partial: str, final: str) -> bool:
        if not partial or not final:
            return False
        if final.startswith(partial) or partial.startswith(final):
            return True
        head_len = min(len(partial), len(final), STREAMING_PREFIX_COMPARISON_LENGTH)
        return partial[:head_len] == final[:head_len]

    def set_final_text_in_timeline(
        self, timeline: list[dict[str, Any]], final_content: str
    ) -> None:
        stripped = final_content.strip()
        if not stripped:
            return
        if timeline and timeline[-1].get("type") == "text":
            last = timeline[-1].get("data", "").strip()
            if last == stripped:
                return
            if self.is_streaming_prefix_of_final(last, stripped):
                timeline[-1] = {"type": "text", "data": stripped}
                return
        timeline.append({"type": "text", "data": stripped})

    @staticmethod
    def upsert_tool_event(
        tool_events: list[dict[str, Any]], mapped: dict[str, Any]
    ) -> None:
        if mapped["type"] == "info":
            data = str(mapped.get("data", ""))
            if _TOOL_INPUT_RE.match(data) or _TOOL_RESULT_RE.match(data):
                return
            tool_events.append(mapped)
            return

        if mapped["type"] in ("tool", "tool_result"):
            tool_use_id = mapped.get("toolUseId")
            for i, existing in enumerate(tool_events):
                if (
                    existing.get("type") == mapped["type"]
                    and existing.get("toolUseId") == tool_use_id
                ):
                    tool_events[i] = mapped
                    return
            if mapped["type"] == "tool":
                tool_name = mapped.get("tool")
                if tool_name:
                    for i in range(len(tool_events) - 1, -1, -1):
                        existing = tool_events[i]
                        if (
                            existing.get("type") == "tool"
                            and existing.get("tool") == tool_name
                        ):
                            if mapped.get("toolUseId") and mapped["toolUseId"] != tool_name:
                                tool_events[i] = mapped
                            else:
                                tool_events[i] = {**existing, **mapped}
                            return
        tool_events.append(mapped)

    def track_tool_event(
        self,
        tool_events: list[dict[str, Any]],
        tool_meta: dict[str, dict[str, Any]],
        mapped: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events_to_emit = [mapped]
        tool_use_id = mapped.get("toolUseId")

        if mapped["type"] == "tool":
            tool_meta[tool_use_id] = {
                "tool": mapped.get("tool"),
                "input": mapped.get("input", {}),
                "mcpServer": mapped.get("mcpServer"),
                "skillName": mapped.get("skillName"),
            }
            self.upsert_tool_event(tool_events, mapped)
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
                    "mcpServer": meta.get("mcpServer"),
                    "skillName": meta.get("skillName"),
                }
                self.upsert_tool_event(tool_events, tool_event)
                events_to_emit.insert(0, tool_event)
            if meta.get("tool"):
                mapped = {**mapped, "tool": meta["tool"]}
                events_to_emit[-1] = mapped
            if "mcpServer" not in mapped and meta.get("mcpServer"):
                mapped = {**mapped, "mcpServer": meta["mcpServer"]}
            if "skillName" not in mapped and meta.get("skillName"):
                mapped = {**mapped, "skillName": meta["skillName"]}
            self.upsert_tool_event(tool_events, mapped)
            return events_to_emit

        if mapped["type"] in ("tool", "tool_result", "info"):
            self.upsert_tool_event(tool_events, mapped)
        return events_to_emit

    @staticmethod
    def normalize_tool_use_id(tool_use_id: str) -> str:
        if tool_use_id.endswith(":input"):
            return tool_use_id[: -len(":input")]
        if tool_use_id.endswith(":result"):
            return tool_use_id[: -len(":result")]
        return tool_use_id

    def map_sink_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event_type = event.get("type")
        data = event.get("data", "")

        if event_type == "markdown":
            return {"type": "token", "data": data}

        if event_type == "text_segment":
            return {"type": "text", "data": data}

        if event_type == "info":
            tool_match = _TOOL_INPUT_RE.match(str(data))
            if tool_match:
                tool_name = tool_match.group(1)
                tool_input = parse_tool_input(tool_match.group(2))
                mapped = {
                    "type": "tool",
                    "tool": tool_name,
                    "input": tool_input,
                    "toolUseId": self.normalize_tool_use_id(
                        event.get("toolUseId", tool_name)
                    ),
                }
                if event.get("mcpServer"):
                    mapped["mcpServer"] = event["mcpServer"]
                if event.get("skillName"):
                    mapped["skillName"] = event["skillName"]
                return mapped
            result_match = _TOOL_RESULT_RE.match(str(data))
            if result_match:
                mapped = {
                    "type": "tool_result",
                    "toolUseId": self.normalize_tool_use_id(event.get("toolUseId", "")),
                    "data": result_match.group(1),
                }
                if event.get("mcpServer"):
                    mapped["mcpServer"] = event["mcpServer"]
                if event.get("skillName"):
                    mapped["skillName"] = event["skillName"]
                return mapped
            return {"type": "info", "data": data}

        return event

    def run_agent_thread(
        self,
        *,
        prompt: str,
        user_id: str,
        mcp_servers: list[str],
        model_name: str,
        skill_list: list[str],
        guardrail_enabled: bool,
        memory_enabled: bool,
        llm_gateway_enabled: bool,
        runtime_session_id: str,
        files: list[str],
        message_queue: queue.Queue,
        result_holder: dict[str, Any],
    ) -> None:
        sink = QueueNotificationSink(message_queue)

        try:
            logger.info("Using AgentCore runtime invoke_agent_runtime")
            response, image_url = run_agent(
                prompt,
                user_id,
                mcp_servers,
                model_name,
                runtime_session_id,
                notification_queue=sink,
                skill_list=skill_list,
                guardrail_enabled=guardrail_enabled,
                memory_enabled=memory_enabled,
                llm_gateway_enabled=llm_gateway_enabled,
                files=files,
            )
            if not isinstance(response, str):
                response = json.dumps(response, ensure_ascii=False)
            result_holder["content"] = response
            result_holder["images"] = image_url or []
        except Exception as exc:
            logger.exception("Agent run failed")
            result_holder["error"] = CLIENT_SAFE_AGENT_ERROR
            result_holder["error_type"] = type(exc).__name__
        finally:
            message_queue.put(None)

    def start_chat_stream(
        self,
        *,
        task_id: str,
        task: dict[str, Any],
        user_id: str,
        prompt: str,
        files: list[str],
    ) -> tuple[queue.Queue, dict[str, Any]]:
        """Configure chat, persist the user turn, and start the agent worker."""
        chat.user_id = user_id
        chat.update(task["model_name"])

        task_store.add_message(task_id, "user", prompt, user_id=user_id, images=files)

        logger.info(
            "chat stream files user=%s files=%s prompt_chars=%s",
            user_id,
            len(files),
            len(prompt),
        )

        message_queue: queue.Queue = queue.Queue()
        result_holder: dict[str, Any] = {"content": "", "images": []}

        self.start_agent_worker(
            prompt=prompt,
            user_id=user_id,
            mcp_servers=task["mcp_servers"],
            model_name=task["model_name"],
            skill_list=task["skills"],
            guardrail_enabled=task["guardrail_enabled"],
            memory_enabled=task["memory_enabled"],
            llm_gateway_enabled=bool(task.get("llm_gateway_enabled", False)),
            runtime_session_id=task["runtime_session_id"],
            files=files,
            message_queue=message_queue,
            result_holder=result_holder,
        )

        return message_queue, result_holder

    def start_agent_worker(self, **kwargs: Any) -> threading.Thread:
        worker = threading.Thread(
            target=self.run_agent_thread,
            kwargs=kwargs,
            daemon=True,
        )
        worker.start()
        return worker

    def _consume_queue_item(
        self,
        item: dict[str, Any],
        *,
        tool_events: list[dict[str, Any]],
        tool_meta: dict[str, dict[str, Any]],
        streamed_text: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Apply one notification-queue item to the timeline. Returns (text, emit)."""
        mapped = self.map_sink_event(item)
        if not mapped:
            return streamed_text, []

        if mapped["type"] == "token":
            before = streamed_text
            streamed_text, committed = self.handle_token(
                tool_events, streamed_text, mapped["data"]
            )
            events: list[dict[str, Any]] = []
            if streamed_text == before and committed is None:
                return streamed_text, events
            if committed:
                events.append(committed)
            events.append(mapped)
            return streamed_text, events

        if mapped["type"] == "text":
            committed = self.flush_text_segment(tool_events, mapped["data"])
            events = [committed] if committed else []
            return "", events

        if mapped["type"] in ("tool", "tool_result", "info"):
            events = []
            if mapped["type"] in ("tool", "tool_result"):
                committed = self.flush_text_segment(tool_events, streamed_text)
                if mapped["type"] == "tool":
                    streamed_text = ""
                if committed:
                    events.append(committed)
            events.extend(self.track_tool_event(tool_events, tool_meta, mapped))
            return streamed_text, events

        return streamed_text, [mapped]

    def _build_final_payload(
        self,
        *,
        result_holder: dict[str, Any],
        tool_events: list[dict[str, Any]],
        streamed_text: str,
    ) -> tuple[str, list[str], list[dict[str, Any]]]:
        authoritative_final = (result_holder.get("content") or "").strip()
        final_content = authoritative_final or streamed_text
        if authoritative_final:
            self.set_final_text_in_timeline(tool_events, final_content)
        else:
            self.flush_text_segment(tool_events, streamed_text)
            self.set_final_text_in_timeline(tool_events, final_content)
        images = result_holder.get("images") or []
        return final_content, images, tool_events

    def build_partial_error_payload(
        self,
        *,
        tool_events: list[dict[str, Any]],
        streamed_text: str,
        error_text: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Keep streamed progress on timeout/error; append an error notice."""
        events = list(tool_events)
        partial = (streamed_text or "").strip()
        if partial:
            self.flush_text_segment(events, partial)
        notice = (
            error_text if error_text.startswith("Error:") else f"Error: {error_text}"
        )
        text_parts = [
            str(e.get("data") or "").strip()
            for e in events
            if e.get("type") == "text" and str(e.get("data") or "").strip()
        ]
        body = text_parts[-1] if text_parts else partial
        content = f"{body}\n\n{notice}".strip() if body else notice
        events.append({"type": "info", "data": notice})
        return content, events

    def _spawn_late_persist(
        self,
        *,
        message_queue: queue.Queue,
        result_holder: dict[str, Any],
        tool_events: list[dict[str, Any]],
        tool_meta: dict[str, dict[str, Any]],
        streamed_text: str,
        on_assistant_done: Any,
        on_flush: Any,
    ) -> None:
        """Keep draining the agent queue after SSE ends; persist the final answer."""

        def _late_persist() -> None:
            text = streamed_text
            deadline = time.monotonic() + LATE_PERSIST_WAIT_SECONDS
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "Late persist timed out waiting for agent worker"
                        )
                        return
                    try:
                        item = message_queue.get(timeout=min(5.0, remaining))
                    except queue.Empty:
                        continue
                    if item is None:
                        break
                    text, _ = self._consume_queue_item(
                        item,
                        tool_events=tool_events,
                        tool_meta=tool_meta,
                        streamed_text=text,
                    )

                if "error" in result_holder:
                    logger.info(
                        "Late persist skipped: agent worker error=%s",
                        result_holder.get("error"),
                    )
                    return

                final_content, images, events = self._build_final_payload(
                    result_holder=result_holder,
                    tool_events=tool_events,
                    streamed_text=text,
                )
                if not (final_content or events):
                    logger.info("Late persist skipped: empty final payload")
                    return

                logger.info(
                    "Late persist saving assistant message (%s chars, %s events)",
                    len(final_content),
                    len(events),
                )
                on_assistant_done(final_content, images, events)
                on_flush()
            except Exception:
                logger.exception("Late persist failed")

        threading.Thread(
            target=_late_persist,
            name="chat-late-persist",
            daemon=True,
        ).start()

    def iter_sse_events(
        self,
        *,
        message_queue: queue.Queue,
        result_holder: dict[str, Any],
        on_assistant_error: Any,
        on_assistant_done: Any,
        on_flush: Any,
    ) -> Generator[str, None, None]:
        """Yield SSE frames until the agent worker finishes."""
        tool_events: list[dict[str, Any]] = []
        tool_meta: dict[str, dict[str, Any]] = {}
        streamed_text = ""
        sse_closed_early = False

        try:
            started_at = time.monotonic()
            # Idle deadline resets whenever the agent emits something.
            deadline = started_at + self.stream_timeout
            max_deadline = started_at + self.stream_max_seconds
            while True:
                now = time.monotonic()
                hard_remaining = max_deadline - now
                idle_remaining = deadline - now
                remaining = min(hard_remaining, idle_remaining)
                if remaining <= 0:
                    timed_out_idle = idle_remaining <= 0
                    elapsed = int(now - started_at)
                    logger.warning(
                        "Agent SSE stream timed out after %ss (%s); scheduling late persist",
                        elapsed,
                        "idle" if timed_out_idle else "max",
                    )
                    content, events = self.build_partial_error_payload(
                        tool_events=tool_events,
                        streamed_text=streamed_text,
                        error_text=CLIENT_SAFE_AGENT_TIMEOUT,
                    )
                    on_assistant_done(content, [], events)
                    yield sse_event(
                        {"type": "error", "data": CLIENT_SAFE_AGENT_TIMEOUT}
                    )
                    yield sse_event(
                        {
                            "type": "done",
                            "content": content,
                            "images": [],
                            "tool_events": events,
                        }
                    )
                    sse_closed_early = True
                    self._spawn_late_persist(
                        message_queue=message_queue,
                        result_holder=result_holder,
                        tool_events=tool_events,
                        tool_meta=tool_meta,
                        streamed_text=streamed_text,
                        on_assistant_done=on_assistant_done,
                        on_flush=on_flush,
                    )
                    return

                try:
                    item = message_queue.get(
                        timeout=min(self.heartbeat_interval, remaining)
                    )
                except queue.Empty:
                    yield sse_keepalive()
                    continue

                if item is None:
                    break

                # Progress received — extend idle window.
                deadline = time.monotonic() + self.stream_timeout

                streamed_text, events_to_emit = self._consume_queue_item(
                    item,
                    tool_events=tool_events,
                    tool_meta=tool_meta,
                    streamed_text=streamed_text,
                )
                for event in events_to_emit:
                    yield sse_event(event)

            if "error" in result_holder:
                safe_error = result_holder.get("error") or CLIENT_SAFE_AGENT_ERROR
                content, events = self.build_partial_error_payload(
                    tool_events=tool_events,
                    streamed_text=streamed_text,
                    error_text=safe_error,
                )
                on_assistant_done(content, [], events)
                yield sse_event({"type": "error", "data": safe_error})
                yield sse_event(
                    {
                        "type": "done",
                        "content": content,
                        "images": [],
                        "tool_events": events,
                    }
                )
                return

            final_content, images, events = self._build_final_payload(
                result_holder=result_holder,
                tool_events=tool_events,
                streamed_text=streamed_text,
            )

            on_assistant_done(final_content, images, events)

            yield sse_event(
                {
                    "type": "done",
                    "content": final_content,
                    "images": images,
                    "tool_events": events,
                }
            )
        except GeneratorExit:
            # Client disconnected (refresh/navigation) while the agent is still running.
            already_final = bool((result_holder.get("content") or "").strip()) or (
                "error" in result_holder
            )
            if not sse_closed_early and not already_final:
                logger.warning(
                    "SSE client disconnected before agent finished; scheduling late persist"
                )
                self._spawn_late_persist(
                    message_queue=message_queue,
                    result_holder=result_holder,
                    tool_events=tool_events,
                    tool_meta=tool_meta,
                    streamed_text=streamed_text,
                    on_assistant_done=on_assistant_done,
                    on_flush=on_flush,
                )
            raise
        finally:
            if not sse_closed_early:
                on_flush()
