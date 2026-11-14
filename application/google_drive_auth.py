"""Per-user Google Drive OAuth access tokens for Drive MCP.

Stores tokens under ``{SESSION_STORAGE_DIR}/{user}/google_drive_oauth.json``.
AgentCore Runtime receives the access token via the invoke payload (same pattern
as ``llm_gateway_key``); the Runtime cannot read this app-local file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("google_drive_auth")

DRIVE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)
DRIVE_SCOPE_STRING = " ".join(DRIVE_SCOPES)
# Re-auth a bit before Google's typical 3600s expiry.
EXPIRY_SKEW_SECONDS = 120


def _utils():
    try:
        from application import utils
    except ImportError:
        import utils  # type: ignore
    return utils


def token_path(user_id: str) -> str:
    utils = _utils()
    segment = utils.sanitize_user_path_segment(user_id) or "default"
    return os.path.join(utils.SESSION_STORAGE_DIR, segment, "google_drive_oauth.json")


def _now() -> float:
    return time.time()


def _read(user_id: str) -> dict[str, Any] | None:
    path = token_path(user_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and (raw.get("access_token") or "").strip():
            return raw
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read Google Drive token for %s: %s", user_id, e)
    return None


def _write(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    utils = _utils()
    segment = utils.sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError("Invalid user_id for Google Drive token path")
    user_dir = os.path.join(utils.SESSION_STORAGE_DIR, segment)
    os.makedirs(user_dir, exist_ok=True)
    path = token_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


def save_access_token(
    user_id: str,
    access_token: str,
    *,
    expires_in: int | None = None,
    scope: str | None = None,
    token_type: str | None = None,
) -> dict[str, Any]:
    token = (access_token or "").strip()
    if not token:
        raise ValueError("access_token is required")
    ttl = int(expires_in) if expires_in and int(expires_in) > 0 else 3600
    expires_at = _now() + ttl
    data = {
        "access_token": token,
        "expires_at": expires_at,
        "expires_in": ttl,
        "scope": (scope or DRIVE_SCOPE_STRING).strip(),
        "token_type": (token_type or "Bearer").strip() or "Bearer",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(user_id, data)
    logger.info(
        "Saved Google Drive access token for %s (expires_in=%ss)",
        user_id,
        ttl,
    )
    return status_for_user(user_id)


def clear_token(user_id: str) -> None:
    path = token_path(user_id)
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("Cleared Google Drive token for %s", user_id)
    except OSError as e:
        logger.warning("Failed to clear Google Drive token for %s: %s", user_id, e)


def is_token_fresh(record: dict[str, Any] | None, *, skew: int = EXPIRY_SKEW_SECONDS) -> bool:
    if not record:
        return False
    token = (record.get("access_token") or "").strip()
    if not token:
        return False
    expires_at = record.get("expires_at")
    try:
        exp = float(expires_at)
    except (TypeError, ValueError):
        return False
    return exp > (_now() + max(0, skew))


def get_access_token(user_id: str | None, *, allow_expired: bool = False) -> Optional[str]:
    if not user_id or not str(user_id).strip():
        return None
    record = _read(str(user_id).strip())
    if not record:
        return None
    if not allow_expired and not is_token_fresh(record):
        return None
    return (record.get("access_token") or "").strip() or None


def status_for_user(user_id: str | None) -> dict[str, Any]:
    if not user_id or not str(user_id).strip():
        return {
            "connected": False,
            "fresh": False,
            "expires_at": None,
            "scope": None,
            "scopes": list(DRIVE_SCOPES),
        }
    record = _read(str(user_id).strip())
    if not record:
        return {
            "connected": False,
            "fresh": False,
            "expires_at": None,
            "scope": None,
            "scopes": list(DRIVE_SCOPES),
        }
    expires_at = record.get("expires_at")
    try:
        exp = float(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        exp = None
    fresh = is_token_fresh(record)
    return {
        "connected": True,
        "fresh": fresh,
        "expires_at": exp,
        "scope": record.get("scope"),
        "scopes": list(DRIVE_SCOPES),
        "updated_at": record.get("updated_at"),
    }


def mcp_selected(mcp_servers: list | None) -> bool:
    if not mcp_servers:
        return False
    names = {str(s).strip().lower() for s in mcp_servers if str(s).strip()}
    return bool(names & {"drive", "google-drive", "google drive", "google_drive"})
