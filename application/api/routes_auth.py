import json
import logging
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

try:
    from application import utils
except ImportError:
    import utils

logger = logging.getLogger("routes_auth")

router = APIRouter(prefix="/api/session", tags=["session"])

SESSION_COOKIE = "agent_user_id"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class SessionRequest(BaseModel):
    credential: str | None = Field(
        default=None, description="Google ID Token (JWT)"
    )
    user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Local-only user id when auth bypass is enabled",
    )


class SessionResponse(BaseModel):
    user_id: str
    name: str | None = None
    picture: str | None = None


def _google_client_id() -> str:
    cfg = utils.load_config()
    client_id = (cfg.get("google_client_id") or "").strip()
    if not client_id:
        raise HTTPException(status_code=500, detail="google_client_id is not configured")
    return client_id


def _env_bypass_flag() -> bool:
    return os.environ.get("ALLOW_LOCAL_AUTH_BYPASS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_loopback_request(request: Request) -> bool:
    """True only for direct localhost access. Ignores X-Forwarded-Host."""
    host = (request.headers.get("host") or "").split("%")[0]
    hostname = host.split(":")[0].strip().lower().strip("[]")
    return hostname in {"localhost", "127.0.0.1", "::1"}


def local_auth_bypass_enabled(request: Request) -> bool:
    """Dev-only bypass: env flag AND loopback Host. Never trust proxies alone."""
    return _env_bypass_flag() and is_loopback_request(request)


def verify_google_token(token: str, client_id: str) -> dict:
    """Verify Google ID Token via tokeninfo (no extra dependency)."""
    url = f"{TOKENINFO_URL}?id_token={token}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            idinfo = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Token verification failed ({e.code}): {body}") from e
    except Exception as e:
        raise ValueError(f"Token verification request failed: {e}") from e

    if idinfo.get("aud") != client_id:
        raise ValueError(f"Invalid audience: {idinfo.get('aud')}")
    if idinfo.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError(f"Invalid issuer: {idinfo.get('iss')}")
    email = (idinfo.get("email") or "").strip()
    if not email:
        raise ValueError("Token does not contain email")
    if idinfo.get("email_verified") in ("false", False):
        raise ValueError("Email is not verified")
    return idinfo


def _cookie_secure(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto == "https"


def _set_user_cookie(response: Response, request: Request, user_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=user_id,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        max_age=60 * 60 * 24 * 365,
    )


@router.post("", response_model=SessionResponse)
def set_session(body: SessionRequest, request: Request, response: Response) -> SessionResponse:
    credential = (body.credential or "").strip()
    local_user_id = (body.user_id or "").strip()

    if credential:
        try:
            idinfo = verify_google_token(credential, _google_client_id())
        except ValueError as e:
            logger.warning("Google login rejected: %s", e)
            raise HTTPException(status_code=401, detail="Invalid Google credential") from e

        user_id = idinfo["email"].strip()
        _set_user_cookie(response, request, user_id)
        logger.info("Google login success: %s", user_id)
        return SessionResponse(
            user_id=user_id,
            name=(idinfo.get("name") or None),
            picture=(idinfo.get("picture") or None),
        )

    if local_user_id:
        if not local_auth_bypass_enabled(request):
            raise HTTPException(
                status_code=403,
                detail="Local auth bypass is disabled",
            )
        _set_user_cookie(response, request, local_user_id)
        logger.warning("Local auth bypass login: %s", local_user_id)
        return SessionResponse(user_id=local_user_id)

    raise HTTPException(status_code=400, detail="credential or user_id is required")


@router.get("", response_model=SessionResponse | None)
def get_session(request: Request) -> SessionResponse | None:
    user_id = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if not user_id:
        return None
    return SessionResponse(user_id=user_id)


@router.delete("", status_code=204)
def clear_session(request: Request, response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        samesite="lax",
        secure=_cookie_secure(request),
    )


def require_user_id(request: Request) -> str:
    user_id = request.cookies.get(SESSION_COOKIE)
    if not user_id:
        raise HTTPException(status_code=401, detail="User session required")
    return user_id
