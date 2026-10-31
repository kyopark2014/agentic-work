import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type LlmGatewayVerifyResult } from "../api";

interface Props {
  enabled: boolean;
  isAdmin: boolean;
  gatewayConfigured: boolean;
  /** URL/Key 검증·저장 후 호출 (태스크 On/Off는 사이드바 체크박스). */
  onSaved: (uiModels?: string[]) => Promise<void> | void;
  onClose: () => void;
}

export function LlmGatewayModal({
  onSaved,
  onClose,
}: Props) {
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [keyConfigured, setKeyConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.getLlmGateway();
        if (!cancelled) {
          setUrl(data.url || "");
          setKeyConfigured(Boolean(data.key_configured || data.configured));
          setKey("");
        }
      } catch (err) {
        if (!cancelled) {
          // 조회 실패해도 URL/Key 입력 폼은 유지 (신규 설정)
          const msg = err instanceof Error ? err.message : String(err);
          if (!/403|admin|forbidden/i.test(msg)) {
            setError(msg);
          }
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  async function handleConfirm() {
    setError(null);
    setSuccess(null);

    const nextUrl = url.trim();
    const nextKey = key.trim();
    if (!nextUrl) {
      setError("URL이 필요합니다.");
      return;
    }
    if (!nextKey && !keyConfigured) {
      setError("처음 설정 시 Key가 필요합니다.");
      return;
    }

    setBusy(true);
    try {
      const result: LlmGatewayVerifyResult = await api.verifyLlmGateway({
        url: nextUrl,
        key: nextKey,
      });
      if (!result.ok) {
        setError(result.message || "LLM Gateway 모델 확인에 실패했습니다.");
        return;
      }
      setSuccess(
        result.message ||
          "저장되었습니다. Settings의 LLM Gateway 체크박스로 이 태스크에서 켜세요.",
      );
      setKeyConfigured(true);
      setKey("");
      await onSaved(result.ui_models);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return createPortal(
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="llm-gateway-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="modal llm-gateway-modal">
        <h2 id="llm-gateway-title">LLM Gateway</h2>
        <p>
          URL을 확인하고, Key는 변경할 때만 입력하세요(비우면 기존 키 유지).
          모델 목록 조회에 성공하면 저장합니다. 이 태스크에서 쓰려면 Settings의
          LLM Gateway 체크박스를 켜세요.
        </p>

        {loading ? (
          <p className="llm-gateway-muted">설정 불러오는 중…</p>
        ) : (
          <form
            className="llm-gateway-fields"
            onSubmit={(e) => {
              e.preventDefault();
              if (!busy && !loading) void handleConfirm();
            }}
          >
            <label className="llm-gateway-field">
              <span>URL</span>
              <input
                type="text"
                value={url}
                disabled={busy}
                autoComplete="off"
                placeholder="https://gateway.example.com"
                onChange={(e) => setUrl(e.target.value)}
              />
            </label>
            <label className="llm-gateway-field">
              <span>
                Key
                {keyConfigured ? " (저장된 키 있음 — 변경 시에만 입력)" : ""}
              </span>
              <input
                type="password"
                value={key}
                disabled={busy}
                autoComplete="new-password"
                placeholder={
                  keyConfigured ? "비워두면 기존 키 유지" : "vk-..."
                }
                onChange={(e) => setKey(e.target.value)}
              />
            </label>
            <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
          </form>
        )}

        {error && (
          <p className="modal-error" role="alert">
            {error}
          </p>
        )}
        {success && <p className="llm-gateway-success">{success}</p>}

        <div className="modal-actions">
          <button
            type="button"
            className="modal-btn-secondary"
            disabled={busy}
            onClick={onClose}
          >
            취소
          </button>
          <button
            type="button"
            className="send-btn"
            disabled={busy || loading}
            onClick={handleConfirm}
          >
            {busy ? "확인 중…" : "확인"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
