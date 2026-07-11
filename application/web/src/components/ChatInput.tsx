import {
  FormEvent,
  KeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { api } from "../api";

interface Props {
  disabled?: boolean;
  onSend: (text: string) => void;
  onRagUploadComplete?: (message: string) => void;
}

const RAG_ACCEPT = ".pdf,.txt,.md,.csv,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.html,.htm,.json,.py,.js";

export function ChatInput({ disabled, onSend, onRagUploadComplete }: Props) {
  const [value, setValue] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{ left: number; top: number } | null>(
    null,
  );
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const addWrapRef = useRef<HTMLDivElement>(null);
  const menuPortalRef = useRef<HTMLDivElement>(null);
  const addBtnRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function updateMenuPosition() {
    const rect = addBtnRef.current?.getBoundingClientRect();
    if (!rect) return;
    setMenuPosition({
      left: rect.left,
      top: rect.top - 8,
    });
  }

  useEffect(() => {
    if (!menuOpen) {
      setMenuPosition(null);
      return;
    }

    updateMenuPosition();
    window.addEventListener("resize", updateMenuPosition);
    window.addEventListener("scroll", updateMenuPosition, true);

    function onPointerDown(e: MouseEvent) {
      const target = e.target as Node;
      if (addWrapRef.current?.contains(target)) return;
      if (menuPortalRef.current?.contains(target)) return;
      setMenuOpen(false);
    }
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setMenuOpen(false);
    }

    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("resize", updateMenuPosition);
      window.removeEventListener("scroll", updateMenuPosition, true);
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  function submit() {
    const text = value.trim();
    if (!text || disabled || uploading) return;
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

  function openRagUpload() {
    setMenuOpen(false);
    setUploadError(null);
    fileInputRef.current?.click();
  }

  async function onFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || disabled || uploading) return;

    setUploading(true);
    setUploadError(null);
    try {
      const result = await api.uploadToRag(file);
      onRagUploadComplete?.(result.message);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setUploadError(message);
    } finally {
      setUploading(false);
    }
  }

  const inputDisabled = disabled || uploading;

  const menu =
    menuOpen && menuPosition
      ? createPortal(
          <div
            ref={menuPortalRef}
            className="chat-add-menu chat-add-menu-portal"
            role="menu"
            style={{
              left: menuPosition.left,
              top: menuPosition.top,
            }}
          >
            <button
              type="button"
              className="chat-add-menu-item"
              role="menuitem"
              onClick={openRagUpload}
            >
              <span className="chat-add-menu-icon" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 16 16">
                  <path
                    d="M4 2.5h5.5L12 5v8.5a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-11a.5.5 0 0 1 .5-.5Z"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                  />
                  <path
                    d="M9.5 2.5V5H12"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                  />
                </svg>
              </span>
              <span className="chat-add-menu-text">
                <span className="chat-add-menu-label">Upload to RAG</span>
                <span className="chat-add-menu-desc">
                  S3에 업로드하고 Knowledge Base 동기화
                </span>
              </span>
            </button>
          </div>,
          document.body,
        )
      : null;

  return (
    <div className="chat-input-area">
      {uploadError && (
        <div className="chat-upload-error" role="alert">
          {uploadError}
        </div>
      )}
      {uploading && (
        <div className="chat-upload-status" role="status">
          RAG에 업로드하고 동기화하는 중...
        </div>
      )}
      <form className="chat-input-wrap" onSubmit={onSubmit}>
        <input
          ref={fileInputRef}
          type="file"
          className="chat-file-input"
          accept={RAG_ACCEPT}
          onChange={onFileSelected}
          tabIndex={-1}
          aria-hidden="true"
        />
        <textarea
          className="chat-input"
          rows={1}
          placeholder="메시지를 입력하세요..."
          value={value}
          disabled={inputDisabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="chat-input-toolbar">
          <div className="chat-input-add-wrap" ref={addWrapRef}>
            <button
              ref={addBtnRef}
              type="button"
              className="chat-add-btn"
              aria-label="추가"
              aria-expanded={menuOpen}
              disabled={inputDisabled}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
                <path
                  d="M8 3v10M3 8h10"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          </div>
          {menu}
          <button
            className="chat-send-btn"
            type="submit"
            aria-label="전송"
            disabled={inputDisabled || !value.trim()}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M8 12.5V3.5M4.5 7 8 3.5 11.5 7"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </form>
    </div>
  );
}
