import { FormEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { formatBrandTitle } from "../formatBrandTitle";

interface Props {
  clientId: string;
  onCredential: (credential: string) => void;
  onLocalUserId?: (userId: string) => void;
  localAuthBypass?: boolean;
  error?: string | null;
  projectName?: string | null;
}

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (response: { credential: string }) => void;
            auto_select?: boolean;
            context?: string;
            use_fedcm_for_prompt?: boolean;
            error_callback?: (error: { type?: string; message?: string }) => void;
          }) => void;
          renderButton: (
            parent: HTMLElement,
            options: Record<string, string | number | boolean>,
          ) => void;
          disableAutoSelect: () => void;
        };
      };
    };
  }
}

const GSI_SCRIPT_ID = "google-gsi-client";
const GSI_SRC = "https://accounts.google.com/gsi/client";

function loadGsiScript(): Promise<void> {
  if (window.google?.accounts?.id) {
    return Promise.resolve();
  }
  const existing = document.getElementById(GSI_SCRIPT_ID) as HTMLScriptElement | null;
  if (existing) {
    return new Promise((resolve, reject) => {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener(
        "error",
        () => reject(new Error("Google 로그인 스크립트 로드 실패")),
        { once: true },
      );
    });
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.id = GSI_SCRIPT_ID;
    script.src = GSI_SRC;
    script.async = true;
    script.defer = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Google 로그인 스크립트 로드 실패"));
    document.head.appendChild(script);
  });
}

function originHint(): string {
  const origin = window.location.origin;
  return (
    `Google Cloud Console → OAuth 클라이언트 → 승인된 JavaScript 원본에 ` +
    `${origin} 과 http://127.0.0.1:${window.location.port || "8501"} 를 모두 추가하세요. ` +
    `반영까지 수 분이 걸릴 수 있습니다.`
  );
}

export function GoogleLoginModal({
  clientId,
  onCredential,
  onLocalUserId,
  localAuthBypass = false,
  error,
  projectName,
}: Props) {
  const title = formatBrandTitle(projectName ?? "agent");
  const buttonRef = useRef<HTMLDivElement>(null);
  const [scriptError, setScriptError] = useState<string | null>(null);

  useEffect(() => {
    if (!clientId || !buttonRef.current) return;
    let cancelled = false;

    (async () => {
      try {
        await loadGsiScript();
        if (cancelled || !buttonRef.current || !window.google?.accounts?.id) return;

        window.google.accounts.id.initialize({
          client_id: clientId,
          callback: (response) => {
            if (response?.credential) onCredential(response.credential);
          },
          auto_select: false,
          context: "signin",
          // FedCM can fail silently on some local Chrome setups; keep classic path.
          use_fedcm_for_prompt: false,
          error_callback: (err) => {
            if (cancelled) return;
            const msg = err?.message || err?.type || "Google 로그인 오류";
            setScriptError(`${msg}. ${originHint()}`);
          },
        });

        buttonRef.current.innerHTML = "";
        window.google.accounts.id.renderButton(buttonRef.current, {
          type: "standard",
          theme: "filled_blue",
          size: "large",
          text: "signin_with",
          shape: "rectangular",
          logo_alignment: "left",
          width: 320,
        });
      } catch (err) {
        if (!cancelled) {
          setScriptError(err instanceof Error ? err.message : String(err));
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [clientId, onCredential]);

  function handleLocalSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!onLocalUserId) return;
    const form = new FormData(e.currentTarget);
    const userId = String(form.get("user_id") ?? "").trim();
    if (userId) onLocalUserId(userId);
  }

  const displayError = error || scriptError;

  return createPortal(
    <div className="auth-screen">
      <div
        className="modal-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="google-login-title"
      >
        <div className="modal google-login-modal">
          <h2 id="google-login-title">{title}</h2>
          <p>시작하려면 Google 계정으로 로그인하세요.</p>
          {displayError && <p className="modal-error">{displayError}</p>}
          {!clientId && (
            <p className="modal-error">google_client_id가 설정되지 않았습니다.</p>
          )}
          <div className="google-login-button" ref={buttonRef} />

          {localAuthBypass && onLocalUserId && (
            <form className="local-auth-bypass" onSubmit={handleLocalSubmit}>
              <p className="local-auth-bypass-label">
                로컬 개발용 — Google 없이 User ID로 시작
              </p>
              <input
                name="user_id"
                placeholder="예: user01"
                autoComplete="username"
                required
              />
              <button type="submit" className="send-btn">
                로컬로 시작
              </button>
            </form>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
