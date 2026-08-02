import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

try:
    from application import utils
    from application import session_cookie
    from application import cloudfront_cookies
except ImportError:
    import utils
    import session_cookie
    import cloudfront_cookies

logger = logging.getLogger("routes_auth")

router = APIRouter(prefix="/api/session", tags=["session"])

SESSION_COOKIE = "agent_user_id"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class SessionRequest(BaseModel):
    credential: str | None = Field(
        default=None, description="Google ID Token (JWT)"
    )
    access_token: str | None = Field(
        default=None, description="Google OAuth access token"
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
    llm_gateway_ready: bool = False


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
    """Dev-only bypass for local development.

    True when:
    - ALLOW_LOCAL_AUTH_BYPASS is set (./run_local.sh), or
    - the request Host is loopback (localhost / 127.0.0.1)

    ECS/production must not set ALLOW_LOCAL_AUTH_BYPASS.
    """
    return _env_bypass_flag() or is_loopback_request(request)


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


def verify_google_access_token(token: str, client_id: str) -> dict:
    """Verify Google OAuth access token and load profile (email/name/picture)."""
    info_url = f"{TOKENINFO_URL}?access_token={urllib.parse.quote(token)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(info_url), timeout=5) as resp:
            tokeninfo = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Access token verification failed ({e.code}): {body}") from e
    except Exception as e:
        raise ValueError(f"Access token verification request failed: {e}") from e

    audience = (tokeninfo.get("aud") or tokeninfo.get("azp") or "").strip()
    if audience != client_id:
        raise ValueError(f"Invalid access token audience: {audience}")

    userinfo_req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(userinfo_req, timeout=5) as resp:
            profile = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Userinfo request failed ({e.code}): {body}") from e
    except Exception as e:
        raise ValueError(f"Userinfo request failed: {e}") from e

    email = (profile.get("email") or tokeninfo.get("email") or "").strip()
    if not email:
        raise ValueError("Access token profile does not contain email")
    verified = profile.get("email_verified", tokeninfo.get("email_verified"))
    if verified in ("false", False):
        raise ValueError("Email is not verified")

    return {
        "email": email,
        "name": profile.get("name"),
        "picture": profile.get("picture"),
        "sub": profile.get("sub") or tokeninfo.get("sub"),
        "email_verified": True,
    }


def _cookie_secure(request: Request) -> bool:
    # CloudFront→ALB is http-only, so ALB's X-Forwarded-Proto is often "http"
    # even when the viewer used HTTPS. Prefer CloudFront's viewer proto, then
    # treat CloudFront / sharing_url hosts as HTTPS viewers (custom domains too).
    from urllib.parse import urlparse

    proto = (
        request.headers.get("cloudfront-forwarded-proto")
        or request.headers.get("x-forwarded-proto")
        or request.url.scheme
        or ""
    ).lower()
    if proto == "https":
        return True
    host = (request.headers.get("host") or request.url.hostname or "").split(":")[0].lower()
    if host.endswith(".cloudfront.net"):
        return True
    try:
        sharing = (utils.load_config().get("sharing_url") or "").strip()
        parsed = urlparse(sharing)
        if parsed.scheme == "https" and (parsed.hostname or "").lower() == host:
            return True
    except Exception:
        pass
    return False


def _set_user_cookie(response: Response, request: Request, user_id: str) -> None:
    token = session_cookie.sign_session(user_id)
    secure = _cookie_secure(request)
    max_age = session_cookie.session_max_age_seconds()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=max_age,
    )
    # Same CloudFront host serves /artifacts|/docs|/images from S3 with TrustedKeyGroups.
    if not cloudfront_cookies.set_signed_cookies(
        response, secure=secure, max_age=max_age
    ):
        logger.warning(
            "CloudFront signed cookies not attached on login (signing material missing?)"
        )


def get_optional_user_id(request: Request) -> str | None:
    """Return verified user_id from the HMAC session cookie, or None."""
    return session_cookie.verify_session(request.cookies.get(SESSION_COOKIE) or "")


def _has_litellm_virtual_key(user_id: str) -> bool:
    try:
        try:
            from application import litellm_virtual_key
        except ImportError:
            import litellm_virtual_key

        return bool(litellm_virtual_key.get_cached_virtual_key(user_id))
    except Exception:
        return False


def _ensure_litellm_virtual_key(user_id: str) -> bool:
    """Provision LiteLLM virtual key for email logins. Returns True when ready."""
    try:
        try:
            from application import litellm_virtual_key
        except ImportError:
            import litellm_virtual_key

        if not litellm_virtual_key.is_email_user_id(user_id):
            return False
        key = litellm_virtual_key.ensure_virtual_key_on_login(user_id)
        if key:
            logger.info("LiteLLM virtual key ready for %s", user_id)
            return True
        logger.warning("LiteLLM virtual key not available for %s", user_id)
        return False
    except Exception:
        logger.exception("LiteLLM virtual key provisioning failed for %s", user_id)
        return False


@router.post("", response_model=SessionResponse)
def set_session(body: SessionRequest, request: Request, response: Response) -> SessionResponse:
    credential = (body.credential or "").strip()
    access_token = (body.access_token or "").strip()
    local_user_id = (body.user_id or "").strip()

    if credential or access_token:
        try:
            if credential:
                idinfo = verify_google_token(credential, _google_client_id())
            else:
                idinfo = verify_google_access_token(access_token, _google_client_id())
        except ValueError as e:
            logger.warning("Google login rejected: %s", e)
            raise HTTPException(status_code=401, detail="Invalid Google credential") from e

        user_id = idinfo["email"].strip()
        _set_user_cookie(response, request, user_id)
        utils.ensure_user_artifacts_dir(user_id)
        try:
            from application import task_store

            task_store.record_login(
                user_id,
                method="google",
                name=(idinfo.get("name") or None),
                picture=(idinfo.get("picture") or None),
            )
        except Exception:
            logger.exception("Failed to record Google login event")

        gateway_ready = _ensure_litellm_virtual_key(user_id)
        logger.info("Google login success: %s (llm_gateway_ready=%s)", user_id, gateway_ready)
        return SessionResponse(
            user_id=user_id,
            name=(idinfo.get("name") or None),
            picture=(idinfo.get("picture") or None),
            llm_gateway_ready=gateway_ready,
        )

    if local_user_id:
        if not local_auth_bypass_enabled(request):
            raise HTTPException(
                status_code=403,
                detail="Local auth bypass is disabled",
            )
        _set_user_cookie(response, request, local_user_id)
        utils.ensure_user_artifacts_dir(local_user_id)
        try:
            from application import task_store

            task_store.record_login(local_user_id, method="local")
        except Exception:
            logger.exception("Failed to record local login event")
        gateway_ready = _ensure_litellm_virtual_key(local_user_id)
        logger.warning(
            "Local auth bypass login: %s (llm_gateway_ready=%s)",
            local_user_id,
            gateway_ready,
        )
        return SessionResponse(user_id=local_user_id, llm_gateway_ready=gateway_ready)

    raise HTTPException(
        status_code=400, detail="credential, access_token, or user_id is required"
    )


@router.get("", response_model=SessionResponse | None)
def get_session(request: Request, response: Response) -> SessionResponse | None:
    user_id = get_optional_user_id(request)
    if not user_id:
        return None
    # Ensure workspace survives process restarts for an existing cookie session
    utils.ensure_user_artifacts_dir(user_id)
    # Refresh CloudFront signed cookies while the session is still valid.
    if not cloudfront_cookies.set_signed_cookies(
        response,
        secure=_cookie_secure(request),
        max_age=session_cookie.session_max_age_seconds(),
    ):
        logger.warning(
            "CloudFront signed cookies not attached on session refresh "
            "(signing material missing?)"
        )
    return SessionResponse(
        user_id=user_id,
        llm_gateway_ready=_has_litellm_virtual_key(user_id),
    )


@router.delete("", status_code=204)
def clear_session(request: Request, response: Response) -> None:
    secure = _cookie_secure(request)
    response.delete_cookie(
        key=SESSION_COOKIE,
        samesite="lax",
        secure=secure,
    )
    cloudfront_cookies.clear_signed_cookies(response, secure=secure)


def require_user_id(request: Request) -> str:
    user_id = get_optional_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="User session required")
    return user_id
