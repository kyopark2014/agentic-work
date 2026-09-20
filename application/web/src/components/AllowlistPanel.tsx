import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";

interface Props {
  currentUser: string;
  onBack: () => void;
}

export function AllowlistPanel({ currentUser, onBack }: Props) {
  const [ids, setIds] = useState<string[]>([]);
  const [adminIds, setAdminIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [newId, setNewId] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.getAllowlist();
      setIds(Array.isArray(data.ids) ? data.ids : []);
      setAdminIds(Array.isArray(data.admin_ids) ? data.admin_ids : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setIds([]);
      setAdminIds([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onAdd(e: FormEvent) {
    e.preventDefault();
    const value = newId.trim();
    if (!value || busy) return;
    setBusy(true);
    setError("");
    try {
      const data = await api.addAllowlistId(value);
      setIds(data.ids);
      setAdminIds(data.admin_ids || []);
      setNewId("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onRemove(id: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const data = await api.removeAllowlistId(id);
      setIds(data.ids);
      setAdminIds(data.admin_ids || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const me = currentUser.trim().toLowerCase();
  const adminSet = new Set(adminIds.map((x) => x.toLowerCase()));

  return (
    <section className="dashboard-section allowlist-panel" aria-labelledby="allowlist-label">
      <div className="dashboard-section-head">
        <div>
          <h2 id="allowlist-label">등록</h2>
          <p className="dashboard-section-sub">
            Google 로그인 허용 계정 목록입니다. 마스터 계정만 추가·삭제할 수
            있습니다.
          </p>
        </div>
        <div className="allowlist-head-actions">
          <button
            type="button"
            className="sidebar-menu-btn"
            onClick={() => void load()}
            disabled={loading || busy}
          >
            새로고침
          </button>
          <button type="button" className="sidebar-menu-btn" onClick={onBack}>
            ← Dashboard
          </button>
        </div>
      </div>

      <form className="allowlist-add" onSubmit={(e) => void onAdd(e)}>
        <input
          type="email"
          value={newId}
          onChange={(e) => setNewId(e.target.value)}
          placeholder="email@example.com"
          autoComplete="off"
          required
          disabled={busy}
        />
        <button type="submit" disabled={busy || !newId.trim()}>
          {busy ? "처리 중…" : "추가"}
        </button>
      </form>

      {error ? <p className="allowlist-error">{error}</p> : null}

      {loading ? (
        <p className="dashboard-empty">목록을 불러오는 중…</p>
      ) : ids.length === 0 ? (
        <p className="dashboard-empty">등록된 계정이 없습니다.</p>
      ) : (
        <ul className="allowlist-rows">
          {ids.map((id) => {
            const isMe = id.toLowerCase() === me;
            const isMaster = adminSet.has(id.toLowerCase());
            const locked = isMe || isMaster;
            return (
              <li key={id} className="allowlist-row">
                <span className="allowlist-id">
                  {id}
                  {isMaster ? <em className="allowlist-you">마스터</em> : null}
                  {isMe && !isMaster ? (
                    <em className="allowlist-you">나</em>
                  ) : null}
                </span>
                <button
                  type="button"
                  className="sidebar-menu-btn allowlist-remove"
                  disabled={busy || locked}
                  title={
                    isMaster
                      ? "마스터 계정은 삭제할 수 없습니다"
                      : isMe
                        ? "현재 로그인한 계정은 삭제할 수 없습니다"
                        : "삭제"
                  }
                  onClick={() => void onRemove(id)}
                >
                  삭제
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
