"""Admin dashboard API — registrant and access metrics."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request

from application.api.routes_auth import require_user_id
from application import task_store, utils

router = APIRouter(prefix="/api/admin", tags=["admin"])


def get_admin_emails() -> set[str]:
    cfg = utils.load_config()
    emails: list[str] = []
    raw = cfg.get("admin_emails")
    if isinstance(raw, list):
        emails.extend(str(item).strip() for item in raw if str(item).strip())
    elif isinstance(raw, str) and raw.strip():
        emails.extend(part.strip() for part in raw.split(",") if part.strip())

    env = os.environ.get("ADMIN_EMAILS", "").strip()
    if env:
        emails.extend(part.strip() for part in env.split(",") if part.strip())

    if not emails:
        emails = ["kyopark2014@gmail.com"]

    return {email.lower() for email in emails}


def is_admin_user(user_id: str) -> bool:
    return user_id.strip().lower() in get_admin_emails()


def require_admin(request: Request) -> str:
    user_id = require_user_id(request)
    if not is_admin_user(user_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


@router.get("/dashboard")
def get_dashboard(_admin: str = Depends(require_admin)) -> dict:
    return task_store.get_dashboard_stats()
