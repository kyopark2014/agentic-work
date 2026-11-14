import { api } from "./api";

/**
 * Connect the user's Notion (remote MCP) via a server-driven OAuth popup.
 * The popup is opened synchronously (popup blockers) and navigated once the
 * authorize URL is known; the callback page posts {type:"notion-oauth"} back.
 */
export async function connectNotion(): Promise<void> {
  const popup = window.open("about:blank", "notion-oauth", "width=520,height=720");
  if (!popup) throw new Error("팝업이 차단되었습니다. 팝업을 허용해 주세요.");

  let url: string;
  try {
    ({ url } = await api.getNotionAuthorizeUrl());
  } catch (err) {
    popup.close();
    throw err;
  }
  popup.location.href = url;

  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", onMessage);
      window.clearInterval(timer);
      fn();
    };
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; ok?: boolean; message?: string };
      if (data?.type !== "notion-oauth") return;
      finish(() =>
        data.ok ? resolve() : reject(new Error(data.message || "Notion 연결 실패")),
      );
    };
    window.addEventListener("message", onMessage);
    const timer = window.setInterval(() => {
      if (popup.closed) {
        finish(() => reject(new Error("Notion 연결 창이 닫혔습니다.")));
      }
    }, 500);
  });
}
