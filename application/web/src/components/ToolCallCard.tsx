import type { ToolEvent } from "../types";

interface Props {
  event: ToolEvent;
}

export function ToolCallCard({ event }: Props) {
  if (event.type === "tool") {
    return (
      <details className="tool-card" open>
        <summary>Tool: {event.tool}</summary>
        <pre>{JSON.stringify(event.input, null, 2)}</pre>
      </details>
    );
  }
  if (event.type === "tool_result") {
    return (
      <details className="tool-card">
        <summary>Tool result</summary>
        <pre>{event.data}</pre>
      </details>
    );
  }
  return (
    <details className="tool-card">
      <summary>Info</summary>
      <pre>{event.data}</pre>
    </details>
  );
}
