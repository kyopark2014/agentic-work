import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from application.task_store_persistence import (
    flush_persist,
    schedule_persist,
    working_db_path,
)

logger = logging.getLogger(__name__)

_APPLICATION_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_APPLICATION_DIR, "data")
_DB_PATH = working_db_path()

DEFAULT_MODEL = "Claude 4.6 Sonnet"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global _DB_PATH
    _DB_PATH = working_db_path()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              title TEXT,
              runtime_session_id TEXT NOT NULL UNIQUE,
              model_name TEXT,
              skills_json TEXT,
              mcp_servers_json TEXT,
              guardrail_enabled INTEGER DEFAULT 0,
              memory_enabled INTEGER DEFAULT 1,
              llm_gateway_enabled INTEGER DEFAULT 0,
              created_at TEXT,
              updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT,
              images_json TEXT,
              tool_events_json TEXT,
              created_at TEXT,
              FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_user_updated
              ON tasks(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_task_created
              ON messages(task_id, created_at ASC);

            CREATE TABLE IF NOT EXISTS login_events (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              method TEXT NOT NULL,
              name TEXT,
              picture TEXT,
              logged_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_events_logged
              ON login_events(logged_at DESC);
            CREATE INDEX IF NOT EXISTS idx_login_events_user
              ON login_events(user_id, logged_at DESC);
            """
        )
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN pinned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN memory_enabled INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN llm_gateway_enabled INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass


def _after_write() -> None:
    schedule_persist()


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"] or "New task",
        "runtime_session_id": row["runtime_session_id"],
        "model_name": row["model_name"] or DEFAULT_MODEL,
        "skills": json.loads(row["skills_json"] or "[]"),
        "mcp_servers": json.loads(row["mcp_servers_json"] or "[]"),
        "guardrail_enabled": bool(row["guardrail_enabled"]),
        "memory_enabled": bool(row["memory_enabled"]) if "memory_enabled" in row.keys() else True,
        "llm_gateway_enabled": (
            bool(row["llm_gateway_enabled"])
            if "llm_gateway_enabled" in row.keys()
            else False
        ),
        "pinned": bool(row["pinned"]) if "pinned" in row.keys() else False,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "role": row["role"],
        "content": row["content"] or "",
        "images": json.loads(row["images_json"] or "[]"),
        "tool_events": json.loads(row["tool_events_json"] or "[]"),
        "created_at": row["created_at"],
    }


def list_tasks(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ?
            ORDER BY pinned DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    with _connect() as conn:
        if user_id:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def get_task_refreshing(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Return a task, reloading from S3 Files once if missing on this instance."""
    task = get_task(task_id, user_id)
    if task:
        return task
    try:
        from application.task_store_persistence import persistence_enabled, restore_tasks_db

        if not persistence_enabled():
            return None
        restore_tasks_db()
        init_db()
    except Exception:
        return None
    return get_task(task_id, user_id)


def create_task(
    user_id: str,
    *,
    model_name: str | None = None,
    skills: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    guardrail_enabled: bool = False,
    memory_enabled: bool = True,
    llm_gateway_enabled: bool = False,
    title: str = "New task",
) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    runtime_session_id = str(uuid.uuid4())
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
              id, user_id, title, runtime_session_id, model_name,
              skills_json, mcp_servers_json, guardrail_enabled, memory_enabled,
              llm_gateway_enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                user_id,
                title,
                runtime_session_id,
                model_name or DEFAULT_MODEL,
                json.dumps(skills or [], ensure_ascii=False),
                json.dumps(mcp_servers or [], ensure_ascii=False),
                1 if guardrail_enabled else 0,
                1 if memory_enabled else 0,
                1 if llm_gateway_enabled else 0,
                now,
                now,
            ),
        )
    # Flush immediately so sibling ECS tasks / replacements can see the row
    # (debounced persist alone loses creates during rolling deploys).
    flush_persist()
    return get_task(task_id)  # type: ignore[return-value]


def update_task(task_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "title": "title",
        "model_name": "model_name",
        "guardrail_enabled": "guardrail_enabled",
        "memory_enabled": "memory_enabled",
        "llm_gateway_enabled": "llm_gateway_enabled",
        "pinned": "pinned",
    }
    sets: list[str] = []
    values: list[Any] = []

    for key, column in allowed.items():
        if key in fields and fields[key] is not None:
            value = fields[key]
            if key in ("guardrail_enabled", "memory_enabled", "llm_gateway_enabled", "pinned"):
                value = 1 if value else 0
            sets.append(f"{column} = ?")
            values.append(value)

    if "skills" in fields and fields["skills"] is not None:
        sets.append("skills_json = ?")
        values.append(json.dumps(fields["skills"], ensure_ascii=False))

    if "mcp_servers" in fields and fields["mcp_servers"] is not None:
        sets.append("mcp_servers_json = ?")
        values.append(json.dumps(fields["mcp_servers"], ensure_ascii=False))

    if not sets:
        return get_task(task_id, user_id)

    sets.append("updated_at = ?")
    values.append(_now_iso())
    values.extend([task_id, user_id])

    with _connect() as conn:
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            values,
        )
    _after_write()
    return get_task(task_id, user_id)


def delete_task(task_id: str, user_id: str) -> bool:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE task_id = ?", (task_id,))
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
    if cur.rowcount > 0:
        _after_write()
    return cur.rowcount > 0


def list_messages(task_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE task_id = ?
            ORDER BY created_at ASC
            """,
            (task_id,),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def add_message(
    task_id: str,
    role: str,
    content: str,
    *,
    images: list[str] | None = None,
    tool_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message_id = str(uuid.uuid4())
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (
              id, task_id, role, content, images_json, tool_events_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                task_id,
                role,
                content,
                json.dumps(images or [], ensure_ascii=False),
                json.dumps(tool_events or [], ensure_ascii=False),
                now,
            ),
        )
        title_update = None
        if role == "user":
            row = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row and (row["title"] or "New task") in ("New task", ""):
                title_update = content.strip()[:50] or "New task"

        conn.execute(
            "UPDATE tasks SET updated_at = ?"
            + (", title = ?" if title_update else "")
            + " WHERE id = ?",
            ([now, title_update, task_id] if title_update else [now, task_id]),
        )
    _after_write()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    return _row_to_message(row) if row else {}


def record_login(
    user_id: str,
    *,
    method: str = "google",
    name: str | None = None,
    picture: str | None = None,
) -> dict[str, Any]:
    """Record a login for dashboard access metrics."""
    event_id = str(uuid.uuid4())
    now = _now_iso()
    uid = user_id.strip()
    with _connect() as conn:
        prior = conn.execute(
            "SELECT 1 FROM login_events WHERE user_id = ? LIMIT 1",
            (uid,),
        ).fetchone()
        is_new_user = prior is None
        conn.execute(
            """
            INSERT INTO login_events (id, user_id, method, name, picture, logged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, uid, method, name, picture, now),
        )
    _after_write()
    try:
        from application.cloudwatch_user_metrics import publish_login_metrics

        publish_login_metrics(method=method, is_new_user=is_new_user)
    except Exception:
        logger.exception("CloudWatch login metrics publish failed")
    return {
        "id": event_id,
        "user_id": uid,
        "method": method,
        "name": name,
        "picture": picture,
        "logged_at": now,
        "is_new_user": is_new_user,
    }


def get_dashboard_stats(*, recent_limit: int = 50, daily_days: int = 14) -> dict[str, Any]:
    """Aggregate registrant + access metrics for the admin dashboard."""
    with _connect() as conn:
        task_users = conn.execute(
            """
            SELECT
              user_id,
              COUNT(*) AS task_count,
              MIN(created_at) AS first_task_at,
              MAX(updated_at) AS last_task_at
            FROM tasks
            GROUP BY user_id
            """
        ).fetchall()

        message_counts = {
            row["user_id"]: row["message_count"]
            for row in conn.execute(
                """
                SELECT t.user_id AS user_id, COUNT(m.id) AS message_count
                FROM tasks t
                LEFT JOIN messages m ON m.task_id = t.id
                GROUP BY t.user_id
                """
            ).fetchall()
        }

        login_users = conn.execute(
            """
            SELECT
              user_id,
              COUNT(*) AS login_count,
              MIN(logged_at) AS first_login_at,
              MAX(logged_at) AS last_login_at,
              GROUP_CONCAT(DISTINCT method) AS methods
            FROM login_events
            GROUP BY user_id
            """
        ).fetchall()

        total_tasks = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        total_messages = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        total_logins = conn.execute("SELECT COUNT(*) AS c FROM login_events").fetchone()["c"]

        recent_logins = [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "method": row["method"],
                "name": row["name"],
                "picture": row["picture"],
                "logged_at": row["logged_at"],
            }
            for row in conn.execute(
                """
                SELECT id, user_id, method, name, picture, logged_at
                FROM login_events
                ORDER BY logged_at DESC
                LIMIT ?
                """,
                (recent_limit,),
            ).fetchall()
        ]

        daily_rows = conn.execute(
            """
            SELECT
              substr(logged_at, 1, 10) AS day,
              COUNT(*) AS logins,
              COUNT(DISTINCT user_id) AS unique_users
            FROM login_events
            WHERE substr(logged_at, 1, 10) >= date('now', ?)
            GROUP BY substr(logged_at, 1, 10)
            ORDER BY day ASC
            """,
            (f"-{max(daily_days, 1)} days",),
        ).fetchall()

        logins_today = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM login_events
            WHERE substr(logged_at, 1, 10) = date('now')
            """
        ).fetchone()["c"]
        active_today = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS c
            FROM login_events
            WHERE substr(logged_at, 1, 10) = date('now')
            """
        ).fetchone()["c"]
        logins_7d = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM login_events
            WHERE substr(logged_at, 1, 10) >= date('now', '-7 days')
            """
        ).fetchone()["c"]
        active_7d = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS c
            FROM login_events
            WHERE substr(logged_at, 1, 10) >= date('now', '-7 days')
            """
        ).fetchone()["c"]

    by_user: dict[str, dict[str, Any]] = {}
    for row in task_users:
        uid = row["user_id"]
        by_user[uid] = {
            "user_id": uid,
            "task_count": int(row["task_count"] or 0),
            "message_count": int(message_counts.get(uid, 0) or 0),
            "login_count": 0,
            "first_seen": row["first_task_at"],
            "last_active": row["last_task_at"],
            "last_login": None,
            "auth_methods": [],
            "is_google": "@" in uid,
        }
    for row in login_users:
        uid = row["user_id"]
        methods = [
            m.strip()
            for m in (row["methods"] or "").split(",")
            if m and m.strip()
        ]
        entry = by_user.get(uid)
        if not entry:
            entry = {
                "user_id": uid,
                "task_count": 0,
                "message_count": 0,
                "login_count": 0,
                "first_seen": row["first_login_at"],
                "last_active": row["last_login_at"],
                "last_login": row["last_login_at"],
                "auth_methods": methods,
                "is_google": "google" in methods or "@" in uid,
            }
            by_user[uid] = entry
        else:
            entry["login_count"] = int(row["login_count"] or 0)
            entry["last_login"] = row["last_login_at"]
            entry["auth_methods"] = methods
            entry["is_google"] = entry["is_google"] or ("google" in methods)
            if row["first_login_at"] and (
                not entry["first_seen"] or row["first_login_at"] < entry["first_seen"]
            ):
                entry["first_seen"] = row["first_login_at"]
            if row["last_login_at"] and (
                not entry["last_active"] or row["last_login_at"] > entry["last_active"]
            ):
                entry["last_active"] = row["last_login_at"]
        entry["login_count"] = int(row["login_count"] or 0)

    users = sorted(
        by_user.values(),
        key=lambda u: u.get("last_active") or "",
        reverse=True,
    )
    google_users = sum(1 for u in users if u.get("is_google"))
    legacy_users = len(users) - google_users

    return {
        "summary": {
            "total_users": len(users),
            "google_users": google_users,
            "legacy_users": legacy_users,
            "total_tasks": int(total_tasks or 0),
            "total_messages": int(total_messages or 0),
            "total_logins": int(total_logins or 0),
            "logins_today": int(logins_today or 0),
            "active_users_today": int(active_today or 0),
            "logins_7d": int(logins_7d or 0),
            "active_users_7d": int(active_7d or 0),
        },
        "users": users,
        "recent_logins": recent_logins,
        "daily_logins": [
            {
                "date": row["day"],
                "logins": int(row["logins"] or 0),
                "unique_users": int(row["unique_users"] or 0),
            }
            for row in daily_rows
        ],
    }
