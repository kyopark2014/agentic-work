"""Load and enforce the Google-login allowlist."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from functools import lru_cache

from fastapi import HTTPException

from application import utils

logger = logging.getLogger("allow_list")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _candidate_paths() -> list[str]:
    env_path = (os.environ.get("ALLOW_LIST_PATH") or "").strip()
    paths: list[str] = []
    if env_path:
        paths.append(env_path)
    # Persistent mount (ECS /mnt/app-data) — preferred for runtime edits
    storage = (utils.SESSION_STORAGE_DIR or "").strip()
    if storage:
        paths.append(os.path.join(storage, "allowlist.json"))
    # application/allowlist.json
    paths.append(os.path.join(utils.script_dir, "allowlist.json"))
    # repo-root allowlist.json (next to installer.py)
    paths.append(os.path.join(os.path.dirname(utils.script_dir), "allowlist.json"))
    return paths


def _bundled_seed_path() -> str | None:
    for path in (
        os.path.join(utils.script_dir, "allowlist.json"),
        os.path.join(os.path.dirname(utils.script_dir), "allowlist.json"),
    ):
        if os.path.isfile(path):
            return path
    return None


def resolve_allowlist_path() -> str | None:
    """Return the first existing allowlist.json path, or None.

    If a persistent storage dir exists and has no allowlist yet, seed it from
    the bundled repo file so UI edits survive container restarts.
    """
    storage = (utils.SESSION_STORAGE_DIR or "").strip()
    storage_path = (
        os.path.join(storage, "allowlist.json") if storage else ""
    )
    if storage_path and os.path.isfile(storage_path):
        return storage_path

    if storage and os.path.isdir(storage) and storage_path:
        seed = _bundled_seed_path()
        if seed:
            try:
                with open(seed, encoding="utf-8") as f:
                    data = json.load(f)
                os.makedirs(storage, exist_ok=True)
                with open(storage_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                logger.info("Seeded allowlist to persistent storage: %s", storage_path)
                return storage_path
            except Exception as e:
                logger.warning("Failed to seed allowlist to %s: %s", storage_path, e)

    for path in _candidate_paths():
        if path and os.path.isfile(path):
            return path
    return None


def resolve_allowlist_write_path() -> str:
    """Path used when creating or updating the allowlist."""
    existing = resolve_allowlist_path()
    if existing:
        return existing
    storage = (utils.SESSION_STORAGE_DIR or "").strip()
    if storage and os.path.isdir(storage):
        return os.path.join(storage, "allowlist.json")
    return os.path.join(utils.script_dir, "allowlist.json")


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def validate_email(value: str) -> str:
    email = normalize_email(value)
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(
            status_code=400,
            detail="유효한 이메일 주소를 입력하세요.",
        )
    if len(email) > 254:
        raise HTTPException(status_code=400, detail="이메일이 너무 깁니다.")
    return email


@lru_cache(maxsize=1)
def load_allowed_ids() -> frozenset[str]:
    """Return normalized (lowercase) allowed user ids from allowlist.json."""
    path = resolve_allowlist_path()
    if not path:
        logger.error("allowlist.json not found; denying all Google logins")
        return frozenset()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Failed to load allowlist from %s: %s", path, e)
        return frozenset()

    raw_ids: list[str] = []
    if isinstance(data, list):
        raw_ids = [str(x) for x in data]
    elif isinstance(data, dict):
        for key in ("ids", "allowed_ids", "emails", "users"):
            val = data.get(key)
            if isinstance(val, list):
                raw_ids = [str(x) for x in val]
                break

    allowed = frozenset(normalize_email(x) for x in raw_ids if str(x).strip())
    logger.info("Loaded allowlist (%d ids) from %s", len(allowed), path)
    return allowed


def list_allowed_ids() -> list[str]:
    return sorted(load_allowed_ids())


def _write_allowed_ids(ids: set[str]) -> list[str]:
    path = resolve_allowlist_write_path()
    ordered = sorted(normalize_email(x) for x in ids if x)
    payload = {"ids": ordered}
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="allowlist.", suffix=".json", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    load_allowed_ids.cache_clear()
    logger.info("Updated allowlist (%d ids) at %s", len(ordered), path)
    return ordered


def add_allowed_id(email: str) -> list[str]:
    email = validate_email(email)
    current = set(load_allowed_ids())
    if email in current:
        raise HTTPException(status_code=409, detail="이미 등록된 계정입니다.")
    current.add(email)
    return _write_allowed_ids(current)


def remove_allowed_id(email: str, *, actor: str | None = None) -> list[str]:
    email = validate_email(email)
    current = set(load_allowed_ids())
    if email not in current:
        raise HTTPException(status_code=404, detail="등록되지 않은 계정입니다.")
    if actor and normalize_email(actor) == email:
        raise HTTPException(
            status_code=400,
            detail="현재 로그인한 계정은 삭제할 수 없습니다.",
        )
    if len(current) <= 1:
        raise HTTPException(
            status_code=400,
            detail="마지막 등록 계정은 삭제할 수 없습니다.",
        )
    current.remove(email)
    return _write_allowed_ids(current)


def is_allowed_user(user_id: str | None) -> bool:
    if not user_id:
        return False
    return normalize_email(user_id) in load_allowed_ids()


def require_allowed_user(user_id: str) -> str:
    """Raise 403 if user_id is not on the allow list. Returns normalized id."""
    normalized = (user_id or "").strip()
    if not is_allowed_user(normalized):
        logger.warning("Allow-list rejected user: %s", normalized)
        raise HTTPException(
            status_code=403,
            detail="허용되지 않은 계정입니다. 등록된 Google 계정으로 로그인하세요.",
        )
    return normalized
