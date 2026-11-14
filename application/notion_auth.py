"""Per-user Notion (remote MCP) OAuth tokens.

Notion's hosted MCP (``https://mcp.notion.com/mcp``) uses OAuth 2.0 authorization
code + PKCE with dynamic client registration (public client, no secret).

Flow (server side, no JS SDK needed):
  start()   -> discovery, DCR (cached per redirect_uri), PKCE, returns authorize URL
  finish()  -> validates state, exchanges the code, stores tokens
  get_access_token() -> returns a fresh token, refreshing with the refresh token

Tokens are stored under ``{SESSION_STORAGE_DIR}/{user}/notion_oauth.json``.
AgentCore Runtime receives the access token via the invoke payload (same pattern as
``google_drive_auth``); it cannot read this app-local file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("notion_auth")

NOTION_MCP_URL = "https://mcp.notion.com/mcp"
NOTION_ISSUER = "https://mcp.notion.com"
METADATA_URL = f"{NOTION_ISSUER}/.well-known/oauth-authorization-server"
SCOPE = "default"
CLIENT_NAME = "agentic-work"
EXPIRY_SKEW_SECONDS = 120
PENDING_TTL_SECONDS = 600
HTTP_TIMEOUT = 20

_metadata_cache: dict[str, Any] | None = None


def _utils():
    try:
        from application import utils
    except ImportError:
        import utils  # type: ignore
    return utils


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def token_path(user_id: str) -> str:
    utils = _utils()
    segment = utils.sanitize_user_path_segment(user_id) or "default"
    return os.path.join(utils.SESSION_STORAGE_DIR, segment, "notion_oauth.json")


def _app_dir() -> str:
    path = os.path.join(_utils().SESSION_STORAGE_DIR, "_notion_oauth")
    os.makedirs(path, exist_ok=True)
    return path


def _write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s", path, e)
        return None


def _read(user_id: str) -> dict[str, Any] | None:
    rec = _read_json(token_path(user_id))
    if rec and (rec.get("access_token") or "").strip():
        return rec
    return None


def _write(user_id: str, data: dict[str, Any]) -> None:
    segment = _utils().sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError("Invalid user_id for Notion token path")
    _write_json(token_path(user_id), data)


def clear_token(user_id: str) -> None:
    path = token_path(user_id)
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("Cleared Notion token for %s", user_id)
    except OSError as e:
        logger.warning("Failed to clear Notion token for %s: %s", user_id, e)


# --------------------------------------------------------------------------- #
# http helpers
# --------------------------------------------------------------------------- #
def _http_json(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method or ("POST" if data else "GET")
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{url} -> HTTP {e.code}: {detail}") from e
    return json.loads(body) if body else {}


def _metadata() -> dict[str, Any]:
    global _metadata_cache
    if _metadata_cache is None:
        _metadata_cache = _http_json(METADATA_URL)
    return _metadata_cache


def _form(**fields: str) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


# --------------------------------------------------------------------------- #
# dynamic client registration (cached per redirect_uri)
# --------------------------------------------------------------------------- #
def _client_cache_path(redirect_uri: str) -> str:
    digest = hashlib.sha256(redirect_uri.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_app_dir(), f"client_{digest}.json")


def _get_client_id(redirect_uri: str) -> str:
    cached = _read_json(_client_cache_path(redirect_uri))
    if cached and cached.get("client_id") and cached.get("redirect_uri") == redirect_uri:
        return str(cached["client_id"])
    meta = _metadata()
    reg = _http_json(
        meta["registration_endpoint"],
        data=json.dumps(
            {
                "client_name": CLIENT_NAME,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    client_id = str(reg["client_id"])
    _write_json(
        _client_cache_path(redirect_uri),
        {"client_id": client_id, "redirect_uri": redirect_uri},
    )
    logger.info("Registered Notion OAuth client for %s", redirect_uri)
    return client_id


# --------------------------------------------------------------------------- #
# authorize / callback
# --------------------------------------------------------------------------- #
def _pending_path(state: str) -> str:
    safe = "".join(c for c in state if c.isalnum() or c in "-_")
    return os.path.join(_app_dir(), f"pending_{safe}.json")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start(user_id: str, redirect_uri: str) -> str:
    """Begin the flow; returns the URL the browser must open."""
    meta = _metadata()
    client_id = _get_client_id(redirect_uri)
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(24))
    _write_json(
        _pending_path(state),
        {
            "user_id": user_id,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "created_at": _now(),
        },
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": NOTION_MCP_URL,
            "scope": SCOPE,
        }
    )
    return f"{meta['authorization_endpoint']}?{query}"


def _normalize_issuer(value: str) -> str:
    # Notion returns "https://mcp.notion.com" in the redirect but advertises
    # "https://mcp.notion.com/" in some metadata; compare without trailing slash.
    return (value or "").rstrip("/")


def finish(state: str, code: str, iss: str | None = None) -> str:
    """Complete the flow; returns the user_id the token was stored for."""
    if not state or not code:
        raise ValueError("state and code are required")
    pending_file = _pending_path(state)
    pending = _read_json(pending_file)
    if not pending:
        raise ValueError("Unknown or expired OAuth state")
    try:
        os.remove(pending_file)  # single use
    except OSError:
        pass
    if _now() - float(pending.get("created_at", 0)) > PENDING_TTL_SECONDS:
        raise ValueError("OAuth state expired")
    if iss and _normalize_issuer(iss) != _normalize_issuer(NOTION_ISSUER):
        raise ValueError(f"Unexpected issuer in authorization response: {iss}")

    meta = _metadata()
    age = _now() - float(pending.get("created_at", 0))
    try:
        token = _http_json(
            meta["token_endpoint"],
            data=_form(
                grant_type="authorization_code",
                code=code,
                redirect_uri=pending["redirect_uri"],
                client_id=pending["client_id"],
                code_verifier=pending["verifier"],
                resource=NOTION_MCP_URL,
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except Exception:
        # Enough context to tell client/redirect/PKCE/timing problems apart
        # (never log the code, verifier or tokens).
        logger.error(
            "Notion token exchange failed: client_id=%s redirect_uri=%s "
            "code_len=%d code_parts=%d verifier_len=%d age_since_authorize=%.1fs "
            "metadata_issuer=%s",
            pending.get("client_id"),
            pending.get("redirect_uri"),
            len(code),
            code.count(":") + 1,
            len(pending.get("verifier") or ""),
            age,
            meta.get("issuer"),
        )
        raise
    user_id = str(pending["user_id"])
    _save_token_response(user_id, token, client_id=pending["client_id"])
    return user_id


def _save_token_response(
    user_id: str,
    token: dict[str, Any],
    *,
    client_id: str,
    previous: dict[str, Any] | None = None,
) -> None:
    access = (token.get("access_token") or "").strip()
    if not access:
        raise ValueError("Token response has no access_token")
    ttl = int(token.get("expires_in") or 3600)
    refresh = (token.get("refresh_token") or (previous or {}).get("refresh_token") or "").strip()
    _write(
        user_id,
        {
            "access_token": access,
            "refresh_token": refresh,
            "client_id": client_id,
            "expires_at": _now() + ttl,
            "expires_in": ttl,
            "scope": token.get("scope") or SCOPE,
            "token_type": token.get("token_type") or "Bearer",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    logger.info("Saved Notion token for %s (expires_in=%ss, refresh=%s)", user_id, ttl, bool(refresh))


# --------------------------------------------------------------------------- #
# read side
# --------------------------------------------------------------------------- #
def is_token_fresh(record: dict[str, Any] | None, *, skew: int = EXPIRY_SKEW_SECONDS) -> bool:
    if not record or not (record.get("access_token") or "").strip():
        return False
    try:
        exp = float(record.get("expires_at"))
    except (TypeError, ValueError):
        return False
    return exp > (_now() + max(0, skew))


def _refresh(user_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    refresh = (record.get("refresh_token") or "").strip()
    client_id = (record.get("client_id") or "").strip()
    if not refresh or not client_id:
        return None
    try:
        token = _http_json(
            _metadata()["token_endpoint"],
            data=_form(
                grant_type="refresh_token",
                refresh_token=refresh,
                client_id=client_id,
                resource=NOTION_MCP_URL,
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _save_token_response(user_id, token, client_id=client_id, previous=record)
        return _read(user_id)
    except Exception as e:
        logger.warning("Notion token refresh failed for %s: %s", user_id, e)
        return None


def get_access_token(user_id: str | None, *, allow_expired: bool = False) -> Optional[str]:
    """Return a usable access token, refreshing it when it is near expiry."""
    if not user_id or not str(user_id).strip():
        return None
    uid = str(user_id).strip()
    record = _read(uid)
    if not record:
        return None
    if not is_token_fresh(record):
        refreshed = _refresh(uid, record)
        if refreshed:
            record = refreshed
        elif not allow_expired:
            return None
    return (record.get("access_token") or "").strip() or None


def status_for_user(user_id: str | None) -> dict[str, Any]:
    record = _read(str(user_id).strip()) if user_id and str(user_id).strip() else None
    if not record:
        return {"connected": False, "fresh": False, "expires_at": None}
    try:
        exp = float(record.get("expires_at"))
    except (TypeError, ValueError):
        exp = None
    # A refreshable token counts as fresh: it is renewed transparently on use.
    fresh = is_token_fresh(record) or bool((record.get("refresh_token") or "").strip())
    return {
        "connected": True,
        "fresh": fresh,
        "expires_at": exp,
        "updated_at": record.get("updated_at"),
    }


def mcp_selected(mcp_servers: list | None) -> bool:
    if not mcp_servers:
        return False
    names = {str(s).strip().lower() for s in mcp_servers if str(s).strip()}
    return bool(names & {"notion-remote", "notion remote", "notion_remote"})
