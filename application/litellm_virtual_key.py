"""Resolve per-user LiteLLM virtual keys for email user_ids (application / Web UI).

Called on login. Keys are stored as litellm/virtual_key.json:
- ECS: /mnt/app-data/litellm/virtual_key.json (S3 Files prefix app-data/)
- Local with s3_bucket: S3 only
  (s3://{bucket}/app-data/litellm/virtual_key.json) — no local file
- Local offline (no bucket): application/data/litellm/virtual_key.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import boto3
import httpx

try:
    from application import app_data_backend as backend
    from application import utils
except ImportError:
    import app_data_backend as backend  # type: ignore
    import utils

logger = logging.getLogger("litellm_virtual_key")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ENTERPRISE_TEAM_ALIAS = "enterprise"
_USER_ROLE_VIEWER = "internal_user_viewer"
_ALL_MODELS = ["all-proxy-models"]
_MASTER_SECRET_NAME = "litellmmapikey"
_HTTP_TIMEOUT = 30.0

_store_lock = threading.Lock()
# In-memory mirror for S3 backend (never written under application/data/).
_s3_memory_store: dict[str, Any] | None = None


def is_email_user_id(user_id: Optional[str]) -> bool:
    if not user_id or not isinstance(user_id, str):
        return False
    return bool(_EMAIL_RE.match(user_id.strip()))


def virtual_key_location() -> str:
    """Human-readable store location for logs."""
    mode = backend.backend_mode()
    if mode == "mount":
        return backend.persistent_virtual_key_path()
    if mode == "s3":
        bucket, _ = backend.s3_bucket_and_region()
        return f"s3://{bucket}/{backend.virtual_key_s3_key()}"
    return backend.working_virtual_key_path()


def virtual_key_path() -> Path:
    """Local filesystem path when using mount/offline local backends."""
    mode = backend.backend_mode()
    if mode == "mount":
        return Path(backend.persistent_virtual_key_path())
    return Path(backend.working_virtual_key_path())


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".virtual_key.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _read_local_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning("Unexpected virtual_key.json shape at %s; treating as empty", path)
    except Exception as e:
        logger.warning("Could not read %s: %s; treating as empty", path, e)
    return {}


def _load_store_from_s3(*, force_refresh: bool = False) -> dict[str, Any]:
    """Load virtual keys from S3 into memory. Does not create any local file."""
    global _s3_memory_store
    if _s3_memory_store is not None and not force_refresh:
        return _s3_memory_store

    bucket, region = backend.s3_bucket_and_region()
    if not bucket:
        _s3_memory_store = {}
        return _s3_memory_store

    key = backend.virtual_key_s3_key()
    try:
        remote = backend.download_s3_json(bucket, key, region)
        _s3_memory_store = dict(remote) if remote is not None else {}
        logger.info(
            "Loaded virtual_key.json from s3://%s/%s (%d user(s))",
            bucket,
            key,
            len(_s3_memory_store),
        )
    except Exception as e:
        logger.warning("Could not load virtual_key.json from S3: %s", e)
        _s3_memory_store = _s3_memory_store or {}
    return _s3_memory_store


def _save_store_to_s3(data: dict[str, Any]) -> None:
    global _s3_memory_store
    bucket, region = backend.s3_bucket_and_region()
    if not bucket:
        raise RuntimeError("s3_bucket is not configured")
    backend.upload_s3_json(bucket, backend.virtual_key_s3_key(), data, region)
    _s3_memory_store = dict(data)


def _load_virtual_key_store() -> dict[str, Any]:
    mode = backend.backend_mode()
    if mode == "s3":
        return _load_store_from_s3()

    path = virtual_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_local_json(path)
    if not path.exists():
        _atomic_write_json(path, {})
        logger.info("Created virtual key store: %s", path)
        return {}
    return data


def _write_virtual_key_store(data: dict[str, Any]) -> None:
    mode = backend.backend_mode()
    if mode == "s3":
        _save_store_to_s3(data)
        logger.info("Saved LiteLLM virtual key store → %s", virtual_key_location())
        return

    path = virtual_key_path()
    _atomic_write_json(path, data)
    logger.info("Saved LiteLLM virtual key store → %s", path)


def _cached_key_for_user(store: dict[str, Any], user_id: str) -> Optional[str]:
    entry = store.get(user_id)
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, dict):
        key = (entry.get("key") or entry.get("virtual_key") or "").strip()
        return key or None
    return None


def restore_virtual_key_store() -> str:
    """Ensure the virtual key store is loaded (S3 or filesystem)."""
    with _store_lock:
        if backend.backend_mode() == "s3":
            _load_store_from_s3(force_refresh=True)
        else:
            _load_virtual_key_store()
    return virtual_key_location()


def get_cached_virtual_key(user_id: Optional[str]) -> Optional[str]:
    """Return a previously stored virtual key without calling LiteLLM."""
    if not is_email_user_id(user_id):
        return None
    email = user_id.strip()
    with _store_lock:
        store = _load_virtual_key_store()
        return _cached_key_for_user(store, email)


def _save_cached_key(store: dict[str, Any], user_id: str, key: str, **meta: Any) -> None:
    store[user_id] = {
        "key": key,
        "user_id": user_id,
        **{k: v for k, v in meta.items() if v is not None},
    }
    _write_virtual_key_store(store)
    logger.info("Saved LiteLLM virtual key for %s", user_id)


def _load_master_key(config: dict) -> str:
    key = (config.get("litellm_master_key") or "").strip()
    if key:
        return key

    region = config.get("region", "us-west-2")
    secrets_client = boto3.client("secretsmanager", region_name=region)
    response = secrets_client.get_secret_value(SecretId=_MASTER_SECRET_NAME)
    secret_string = response.get("SecretString") or ""
    try:
        secret_data = json.loads(secret_string)
    except json.JSONDecodeError:
        return secret_string.strip()
    key = (secret_data.get("litellm_master_key") or "").strip()
    if not key:
        raise ValueError(f"Secret {_MASTER_SECRET_NAME} has no litellm_master_key")
    return key


def _gateway_url(config: dict) -> str:
    url = (config.get("llm_gateway_url") or "").strip().rstrip("/")
    if not url:
        raise ValueError("llm_gateway_url missing from config.json")
    return url


def _auth_headers(master_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_json(client: httpx.Client, url: str, headers: dict[str, str]) -> Any:
    response = client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def _post_json(client: httpx.Client, url: str, headers: dict[str, str], payload: dict) -> Any:
    response = client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        detail = response.text[:500]
        raise httpx.HTTPStatusError(
            f"{response.status_code} for {url}: {detail}",
            request=response.request,
            response=response,
        )
    if not response.content:
        return {}
    return response.json()


def _find_enterprise_team_id(client: httpx.Client, base_url: str, headers: dict[str, str]) -> str:
    data = _get_json(client, f"{base_url}/team/list", headers)
    teams = data if isinstance(data, list) else data.get("teams") or data.get("data") or []
    for team in teams:
        if not isinstance(team, dict):
            continue
        if (team.get("team_alias") or "").strip() == _ENTERPRISE_TEAM_ALIAS:
            team_id = (team.get("team_id") or "").strip()
            if team_id:
                return team_id
    raise RuntimeError(f"LiteLLM team '{_ENTERPRISE_TEAM_ALIAS}' not found")


def _find_user_by_email(
    client: httpx.Client, base_url: str, headers: dict[str, str], email: str
) -> Optional[dict[str, Any]]:
    data = _get_json(
        client,
        f"{base_url}/user/list?user_email={quote(email)}",
        headers,
    )
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        return None
    for user in users:
        if not isinstance(user, dict):
            continue
        if (user.get("user_email") or "").strip().lower() == email.lower():
            return user
        if (user.get("user_id") or "").strip().lower() == email.lower():
            return user
    return None


def _list_keys_for_user(
    client: httpx.Client, base_url: str, headers: dict[str, str], litellm_user_id: str
) -> list[dict[str, Any]]:
    data = _get_json(
        client,
        f"{base_url}/key/list?user_id={quote(litellm_user_id)}&return_full_object=true&page=1&size=100",
        headers,
    )
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return []
    return [k for k in keys if isinstance(k, dict)]


def _ensure_user(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    email: str,
    team_id: str,
) -> str:
    existing = _find_user_by_email(client, base_url, headers, email)
    if existing:
        litellm_user_id = (existing.get("user_id") or email).strip()
        logger.info("LiteLLM user already exists for %s: %s", email, litellm_user_id)
    else:
        created = _post_json(
            client,
            f"{base_url}/user/new",
            headers,
            {
                "user_id": email,
                "user_email": email,
                "user_role": _USER_ROLE_VIEWER,
                "models": _ALL_MODELS,
                "auto_create_key": False,
                "max_budget": None,
                "tpm_limit": None,
                "rpm_limit": None,
            },
        )
        litellm_user_id = (created.get("user_id") or email).strip()
        logger.info("Created LiteLLM viewer user: %s", litellm_user_id)

    try:
        _post_json(
            client,
            f"{base_url}/team/member_add",
            headers,
            {
                "team_id": team_id,
                "member": {
                    "role": "user",
                    "user_id": litellm_user_id,
                    "user_email": email,
                },
            },
        )
        logger.info("Added %s to team %s", litellm_user_id, _ENTERPRISE_TEAM_ALIAS)
    except httpx.HTTPStatusError as e:
        if "Unique constraint" in str(e) or "already" in str(e).lower():
            logger.info("User %s already on team %s", litellm_user_id, _ENTERPRISE_TEAM_ALIAS)
        else:
            raise

    return litellm_user_id


def _generate_virtual_key(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    *,
    litellm_user_id: str,
    email: str,
    team_id: str,
) -> str:
    payload = {
        "user_id": litellm_user_id,
        "team_id": team_id,
        "key_alias": email,
        "models": _ALL_MODELS,
        "max_budget": None,
        "tpm_limit": None,
        "rpm_limit": None,
        "max_parallel_requests": None,
        "budget_duration": None,
    }
    try:
        created = _post_json(client, f"{base_url}/key/generate", headers, payload)
    except httpx.HTTPStatusError as e:
        if "alias" in str(e).lower() or e.response is not None and e.response.status_code == 400:
            payload["key_alias"] = f"agentic-work:{email}"
            created = _post_json(client, f"{base_url}/key/generate", headers, payload)
        else:
            raise

    key = (created.get("key") or "").strip()
    if not key:
        raise RuntimeError(f"LiteLLM /key/generate returned no key for {email}")
    logger.info(
        "Created LiteLLM virtual key for %s (team=%s, models=%s, role=%s)",
        email,
        _ENTERPRISE_TEAM_ALIAS,
        _ALL_MODELS,
        _USER_ROLE_VIEWER,
    )
    return key


def resolve_virtual_key_for_email(user_id: Optional[str]) -> Optional[str]:
    """Return a LiteLLM virtual key for an email user_id, or None if not applicable."""
    if not is_email_user_id(user_id):
        return None

    email = user_id.strip()
    with _store_lock:
        store = _load_virtual_key_store()
        cached = _cached_key_for_user(store, email)
    if cached:
        logger.info("Using cached LiteLLM virtual key for %s", email)
        return cached

    config = utils.load_config()
    try:
        master_key = _load_master_key(config)
        base_url = _gateway_url(config)
    except Exception as e:
        logger.error("Cannot resolve LiteLLM master/gateway config: %s", e)
        return None

    headers = _auth_headers(master_key)
    team_id = ""

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            team_id = _find_enterprise_team_id(client, base_url, headers)
            existing_user = _find_user_by_email(client, base_url, headers, email)
            if existing_user:
                litellm_user_id = (existing_user.get("user_id") or email).strip()
                remote_keys = _list_keys_for_user(client, base_url, headers, litellm_user_id)
                if remote_keys:
                    logger.info(
                        "LiteLLM already has %d key(s) for %s; "
                        "creating a new virtual key for local cache "
                        "(plaintext keys are not retrievable from the API)",
                        len(remote_keys),
                        email,
                    )
                else:
                    logger.info(
                        "LiteLLM user %s exists but has no keys; creating virtual key",
                        email,
                    )
            else:
                logger.info("No LiteLLM user for %s; creating viewer + virtual key", email)

            litellm_user_id = _ensure_user(client, base_url, headers, email, team_id)
            key = _generate_virtual_key(
                client,
                base_url,
                headers,
                litellm_user_id=litellm_user_id,
                email=email,
                team_id=team_id,
            )
    except Exception as e:
        logger.error("Failed to resolve LiteLLM virtual key for %s: %s", email, e)
        return None

    with _store_lock:
        # Refresh from S3 so we don't clobber concurrent updates when possible.
        if backend.backend_mode() == "s3":
            store = _load_store_from_s3(force_refresh=True)
        else:
            store = _load_virtual_key_store()
        existing = _cached_key_for_user(store, email)
        if existing:
            return existing
        _save_cached_key(store, email, key, team_id=team_id, team_alias=_ENTERPRISE_TEAM_ALIAS)
    return key


def ensure_virtual_key_on_login(user_id: Optional[str]) -> Optional[str]:
    """Best-effort virtual key provisioning at Web UI login. Never raises."""
    try:
        return resolve_virtual_key_for_email(user_id)
    except Exception as e:
        logger.warning("LiteLLM virtual key provisioning on login failed: %s", e)
        return None
