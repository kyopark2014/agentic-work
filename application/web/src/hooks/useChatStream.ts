import { useCallback, useState } from "react";
import type { ToolEvent } from "../types";
import { api } from "../api";
import { uiError, uiLog, uiWarn } from "../debug";

function upsertToolEvent(prev: ToolEvent[], event: ToolEvent): ToolEvent[] {
  if (event.type === "tool" || event.type === "tool_result") {
    const idx = prev.findIndex(
      (e) => e.type === event.type && e.toolUseId === event.toolUseId,
    );
    if (idx >= 0) {
      const next = [...prev];
      next[idx] = event;
      return next;
    }
  }
  return [...prev, event];
}

export function useChatStream() {
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [streamTools, setStreamTools] = useState<ToolEvent[]>([]);

  const sendMessage = useCallback(
    async (taskId: string, prompt: string, onDone: () => void | Promise<void>) => {
      uiLog("chat:send start", { taskId, prompt });
      setStreaming(true);
      setStreamText("");
      setStreamTools([]);

      try {
        for await (const event of api.streamChat(taskId, prompt)) {
          if (event.type === "token" && event.data !== undefined) {
            setStreamText(event.data);
          } else if (
            event.type === "tool" ||
            event.type === "tool_result" ||
            event.type === "info"
          ) {
            setStreamTools((prev) => upsertToolEvent(prev, event as ToolEvent));
          } else if (event.type === "error") {
            const msg = event.data ?? "Unknown error";
            uiError("chat:send stream error", msg);
            setStreamText(msg.startsWith("Error:") ? msg : `Error: ${msg}`);
          } else if (event.type === "done") {
            uiLog("chat:send done event", {
              contentLength: event.content?.length ?? 0,
              images: event.images?.length ?? 0,
            });
          }
        }
      } catch (err) {
        uiError("chat:send failed", err);
        setStreamText(
          `Error: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        try {
          uiLog("chat:send refreshing messages");
          await onDone();
          uiLog("chat:send refresh complete");
        } catch (err) {
          uiWarn("chat:send refresh failed", err);
        } finally {
          setStreaming(false);
          setStreamText("");
          setStreamTools([]);
          uiLog("chat:send finished", { taskId });
        }
      }
    },
    [],
  );

  return { streaming, streamText, streamTools, sendMessage };
}
