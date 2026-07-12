import { useCallback, useRef, useState } from "react";
import type { ToolEvent } from "../types";
import { api } from "../api";
import { uiError, uiLog, uiWarn } from "../debug";

const TOOL_INPUT_INFO_RE = /^Tool: .+?, Input:/s;
const TOOL_RESULT_INFO_RE = /^Tool Result: /s;

function upsertToolEvent(prev: ToolEvent[], event: ToolEvent): ToolEvent[] {
  if (event.type === "info") {
    const data = event.data ?? "";
    if (TOOL_INPUT_INFO_RE.test(data) || TOOL_RESULT_INFO_RE.test(data)) {
      return prev;
    }
  }
  if (event.type === "tool" || event.type === "tool_result") {
    const idx = prev.findIndex(
      (e) => e.type === event.type && e.toolUseId === event.toolUseId,
    );
    if (idx >= 0) {
      const next = [...prev];
      next[idx] = event;
      return next;
    }
    if (event.type === "tool" && event.tool) {
      const byName = prev.findIndex(
        (e) => e.type === "tool" && e.tool === event.tool,
      );
      if (byName >= 0) {
        const next = [...prev];
        next[byName] =
          event.toolUseId && event.toolUseId !== event.tool
            ? event
            : { ...next[byName], ...event };
        return next;
      }
    }
  }
  return [...prev, event];
}

function appendTextSegment(prev: ToolEvent[], text: string): ToolEvent[] {
  const trimmed = text.trim();
  if (!trimmed) return prev;
  const last = prev[prev.length - 1];
  if (last?.type === "text" && last.data === trimmed) return prev;
  return [...prev, { type: "text", data: trimmed }];
}

function isSegmentReset(previous: string, next: string): boolean {
  if (!previous.trim()) return false;
  if (!next) return true;
  return !next.startsWith(previous);
}

export interface ChatFinalMessage {
  content: string;
  images: string[];
  tool_events: ToolEvent[];
}

export function useChatStream() {
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [streamEvents, setStreamEvents] = useState<ToolEvent[]>([]);
  const streamTextRef = useRef("");

  const sendMessage = useCallback(
    async (
      taskId: string,
      prompt: string,
      onDone: (final?: ChatFinalMessage) => void | Promise<void>,
    ) => {
      uiLog("chat:send start", { taskId, prompt });
      setStreaming(true);
      streamTextRef.current = "";
      setStreamText("");
      setStreamEvents([]);

      let finalMessage: ChatFinalMessage | undefined;

      const flushTextSegment = () => {
        const text = streamTextRef.current.trim();
        if (!text) return;
        setStreamEvents((prev) => appendTextSegment(prev, text));
        streamTextRef.current = "";
        setStreamText("");
      };

      const clearStreaming = () => {
        flushTextSegment();
        setStreaming(false);
        streamTextRef.current = "";
        setStreamText("");
        setStreamEvents([]);
      };

      try {
        for await (const event of api.streamChat(taskId, prompt)) {
          if (event.type === "token" && event.data !== undefined) {
            const previous = streamTextRef.current;
            const next = event.data;
            if (isSegmentReset(previous, next)) {
              flushTextSegment();
            }
            streamTextRef.current = next;
            setStreamText(next);
          } else if (event.type === "text" && event.data) {
            setStreamEvents((prev) => appendTextSegment(prev, event.data!));
            streamTextRef.current = "";
            setStreamText("");
          } else if (event.type === "tool") {
            flushTextSegment();
            setStreamEvents((prev) => upsertToolEvent(prev, event as ToolEvent));
          } else if (event.type === "tool_result" || event.type === "info") {
            setStreamEvents((prev) => upsertToolEvent(prev, event as ToolEvent));
          } else if (event.type === "error") {
            const msg = event.data ?? "Unknown error";
            uiError("chat:send stream error", msg);
            finalMessage = {
              content: msg.startsWith("Error:") ? msg : `Error: ${msg}`,
              images: [],
              tool_events: [],
            };
            clearStreaming();
          } else if (event.type === "done") {
            uiLog("chat:send done event", {
              contentLength: event.content?.length ?? 0,
              images: event.images?.length ?? 0,
              toolEvents: event.tool_events?.length ?? 0,
            });
            finalMessage = {
              content: event.content ?? "",
              images: event.images ?? [],
              tool_events: event.tool_events ?? [],
            };
            setStreaming(false);
            streamTextRef.current = "";
            setStreamText("");
          }
        }

        if (!finalMessage) {
          const partial = streamTextRef.current.trim();
          uiError("chat:send stream closed before done", {
            partialLength: partial.length,
          });
          finalMessage = {
            content: partial
              ? `${partial}\n\nError: Connection closed before the response completed. Try again or refresh messages.`
              : "Error: Connection closed before the response completed. The agent may still be running — refresh or try again.",
            images: [],
            tool_events: [],
          };
          setStreaming(false);
          streamTextRef.current = "";
          setStreamText("");
        }
      } catch (err) {
        uiError("chat:send failed", err);
        finalMessage = {
          content: `Error: ${err instanceof Error ? err.message : String(err)}`,
          images: [],
          tool_events: [],
        };
        clearStreaming();
      } finally {
        try {
          uiLog("chat:send refreshing messages");
          await onDone(finalMessage);
          uiLog("chat:send refresh complete");
        } catch (err) {
          uiWarn("chat:send refresh failed", err);
        } finally {
          setStreamEvents([]);
          uiLog("chat:send finished", { taskId });
        }
      }
    },
    [],
  );

  return { streaming, streamText, streamEvents, sendMessage };
}
