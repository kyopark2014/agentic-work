import { useCallback, useState } from "react";
import type { ToolEvent } from "../types";
import { api } from "../api";

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
    async (taskId: string, prompt: string, onDone: () => void) => {
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
            setStreamText(event.data ?? "Unknown error");
          }
        }
      } catch (err) {
        setStreamText(
          `Error: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        setStreaming(false);
        setStreamText("");
        setStreamTools([]);
        onDone();
      }
    },
    [],
  );

  return { streaming, streamText, streamTools, sendMessage };
}
