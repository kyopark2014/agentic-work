import { FormEvent } from "react";

interface Props {
  onSubmit: (userId: string) => void;
}

export function UserIdModal({ onSubmit }: Props) {
  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const userId = String(form.get("user_id") ?? "").trim();
    if (userId) onSubmit(userId);
  }

  return (
    <div className="modal-overlay">
      <form className="modal" onSubmit={handleSubmit}>
        <h2>User ID 입력</h2>
        <p>시작하려면 User ID를 입력하세요.</p>
        <input name="user_id" placeholder="예: user01" autoFocus required />
        <div className="modal-actions">
          <button type="submit" className="send-btn">
            시작
          </button>
        </div>
      </form>
    </div>
  );
}
