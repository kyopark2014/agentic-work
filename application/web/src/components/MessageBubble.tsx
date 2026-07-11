import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message, ToolEvent } from "../types";
import { ToolCallCard } from "./ToolCallCard";

interface Props {
  role: "user" | "assistant";
  content: string;
  images?: string[];
  toolEvents?: ToolEvent[];
}

export function MessageBubble({ role, content, images = [], toolEvents = [] }: Props) {
  return (
    <div className={`message-row ${role}`}>
      {toolEvents.map((event, i) => (
        <ToolCallCard key={`${event.type}-${i}`} event={event} />
      ))}
      <div className="message-bubble">
        {role === "assistant" ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        ) : (
          content
        )}
      </div>
      {images.length > 0 && (
        <div className="message-images">
          {images.map((url) => (
            <img key={url} src={url} alt="" />
          ))}
        </div>
      )}
    </div>
  );
}

export function MessageFromRecord({ message }: { message: Message }) {
  return (
    <MessageBubble
      role={message.role}
      content={message.content}
      images={message.images}
      toolEvents={message.tool_events}
    />
  );
}
