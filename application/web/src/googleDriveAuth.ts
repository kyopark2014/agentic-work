import { api, type GoogleDriveStatus } from "./api";

export const DEFAULT_DRIVE_SCOPES = [
  "https://www.googleapis.com/auth/drive.readonly",
  "https://www.googleapis.com/auth/drive.file",
];

type TokenClient = {
  requestAccessToken: (overrideConfig?: { prompt?: string }) => void;
};

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: Record<string, unknown>) => void;
          disableAutoSelect: () => void;
        };
        oauth2: {
          initTokenClient: (config: {
            client_id: string;
            scope: string;
            callback: (response: {
              access_token?: string;
              expires_in?: number;
              scope?: string;
              token_type?: string;
              error?: string;
              error_description?: string;
            }) => void;
            error_callback?: (error: { type?: string; message?: string }) => void;
          }) => TokenClient;
        };
      };
    };
  }
}

const GSI_SCRIPT_ID = "google-gsi-client";
const GSI_SRC = "https://accounts.google.com/gsi/client";

function loadGsiScript(): Promise<void> {
  if (window.google?.accounts?.oauth2) {
    return Promise.resolve();
  }
  const existing = document.getElementById(GSI_SCRIPT_ID) as HTMLScriptElement | null;
  if (existing) {
    return new Promise((resolve, reject) => {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener(
        "error",
        () => reject(new Error("Google 스크립트 로드 실패")),
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
    script.onerror = () => reject(new Error("Google 스크립트 로드 실패"));
    document.head.appendChild(script);
  });
}

export async function requestGoogleDriveAccessToken(
  clientId: string,
  scopes: string[] = DEFAULT_DRIVE_SCOPES,
  prompt: "consent" | "" = "consent",
): Promise<{
  access_token: string;
  expires_in?: number;
  scope?: string;
  token_type?: string;
}> {
  if (!clientId.trim()) {
    throw new Error("google_client_id가 설정되지 않았습니다.");
  }
  await loadGsiScript();
  if (!window.google?.accounts?.oauth2) {
    throw new Error("Google OAuth 클라이언트를 초기화할 수 없습니다.");
  }

  return new Promise((resolve, reject) => {
    const client = window.google!.accounts.oauth2.initTokenClient({
      client_id: clientId,
      scope: scopes.join(" "),
      callback: (response) => {
        if (response?.error) {
          reject(
            new Error(response.error_description || response.error || "Google Drive 권한 요청 실패"),
          );
          return;
        }
        const token = response?.access_token?.trim();
        if (!token) {
          reject(new Error("Google Drive 액세스 토큰을 받지 못했습니다."));
          return;
        }
        resolve({
          access_token: token,
          expires_in: response.expires_in,
          scope: response.scope,
          token_type: response.token_type,
        });
      },
      error_callback: (err) => {
        reject(new Error(err?.message || err?.type || "Google Drive 권한 요청 오류"));
      },
    });
    client.requestAccessToken({ prompt });
  });
}

export async function connectGoogleDrive(
  clientId: string,
  scopes?: string[],
): Promise<GoogleDriveStatus> {
  const token = await requestGoogleDriveAccessToken(clientId, scopes);
  return api.connectGoogleDrive(token);
}

export async function refreshGoogleDriveIfNeeded(
  clientId: string,
  options: {
    connected?: boolean;
    fresh?: boolean;
    scopes?: string[];
  } = {},
): Promise<GoogleDriveStatus | null> {
  const { connected, fresh, scopes } = options;
  if (!connected) return null;
  if (fresh) {
    try {
      return await api.getGoogleDriveStatus();
    } catch {
      return null;
    }
  }
  // Near/expired: silent refresh when possible, otherwise consent.
  try {
    const token = await requestGoogleDriveAccessToken(clientId, scopes, "");
    return await api.connectGoogleDrive(token);
  } catch {
    const token = await requestGoogleDriveAccessToken(clientId, scopes, "consent");
    return await api.connectGoogleDrive(token);
  }
}
