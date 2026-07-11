import { FormEvent, KeyboardEvent, useState } from "react";

interface Props {
  disabled?: boolean;
  onSend: (text: string) => void;
}

export function ChatInput({ disabled, onSend }: Props) {
  const [value, setValue] = useState("");

  function submit() {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    submit();
  }

  return (
    <div className="chat-input-area">
      <form className="chat-input-wrap" onSubmit={onSubmit}>
        <textarea
          className="chat-input"
          rows={1}
          placeholder="메시지를 입력하세요..."
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button className="send-btn" type="submit" disabled={disabled || !value.trim()}>
          전송
        </button>
      </form>
    </div>
  );
}
