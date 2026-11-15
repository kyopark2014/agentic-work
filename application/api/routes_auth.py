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
    from application import allow_list
except ImportError:
    import utils
    import session_cookie
    import cloudfront_cookies
    import allow_list

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
    knowledge_graph_enabled: bool = True
    graph_pattern: str = "pattern1"
    google_drive_connected: bool = False
    google_drive_fresh: bool = False
    notion_connected: bool = False
    notion_fresh: bool = False

class SessionSettingsPatch(BaseModel):
    knowledge_graph_enabled: bool | None = None
    graph_pattern: str | None = None


class GoogleDriveConnectRequest(BaseModel):
    access_token: str = Field(..., min_length=1, description="Google OAuth access token with Drive scopes")
    expires_in: int | None = Field(default=None, description="Token lifetime in seconds")
    scope: str | None = Field(default=None, description="Granted OAuth scopes")
    token_type: str | None = Field(default=None, description="Usually Bearer")


class GoogleDriveStatusResponse(BaseModel):
    connected: bool
    fresh: bool
    expires_at: float | None = None
    scope: str | None = None
    scopes: list[str] = Field(default_factory=list)
    updated_at: str | None = None


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
    user_id = session_cookie.verify_session(request.cookies.get(SESSION_COOKIE) or "")
    if not user_id:
        return None
    # Local/dev bypass: do not enforce allowlist on loopback sessions.
    if local_auth_bypass_enabled(request):
        return user_id
    if not allow_list.is_allowed_user(user_id):
        logger.warning("Session user not on allow-list: %s", user_id)
        return None
    return user_id


def _kick_graph_job(user_id: str, *, force: bool = False) -> None:
    """Fire-and-forget background graph extract.

    When force=False, respects fingerprint/cooldown/running lock.
    When force=True (e.g. Knowledge Graph toggled on), bypass those skips.
    """
    if not utils.is_knowledge_graph_enabled(user_id):
        logger.info("Knowledge Graph disabled for %s — skip extract", user_id)
        return
    try:
        from application.graph_jobs import ensure_graph_job

        ensure_graph_job(user_id, force=force)
    except Exception:
        logger.exception("Failed to schedule graph job for %s", user_id)


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


def _google_drive_status(user_id: str) -> dict:
    try:
        try:
            from application import google_drive_auth
        except ImportError:
            import google_drive_auth

        return google_drive_auth.status_for_user(user_id)
    except Exception:
        logger.exception("Failed to load Google Drive status for %s", user_id)
        return {"connected": False, "fresh": False}


def _oauth_status(user_id: str, name: str) -> dict:
    try:
        return _oauth_module(name).status_for_user(user_id)
    except Exception:
        logger.exception("Failed to load %s status for %s", name, user_id)
        return {"connected": False, "fresh": False, "write": False}


def _session_response(
    user_id: str,
    *,
    name: str | None = None,
    picture: str | None = None,
    llm_gateway_ready: bool | None = None,
) -> SessionResponse:
    settings = utils.load_user_settings(user_id)
    drive = _google_drive_status(user_id)
    notion = _oauth_status(user_id, "notion")
    return SessionResponse(
        user_id=user_id,
        name=name,
        picture=picture,
        llm_gateway_ready=(
            _has_litellm_virtual_key(user_id)
            if llm_gateway_ready is None
            else llm_gateway_ready
        ),
        knowledge_graph_enabled=bool(
            settings.get("knowledge_graph_enabled", True)
        ),
        graph_pattern=utils.normalize_graph_pattern(
            settings.get("graph_pattern", utils.DEFAULT_GRAPH_PATTERN)
        ),
        google_drive_connected=bool(drive.get("connected")),
        google_drive_fresh=bool(drive.get("fresh")),
        notion_connected=bool(notion.get("connected")),
        notion_fresh=bool(notion.get("fresh")),
    )


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

        user_id = allow_list.require_allowed_user(idinfo["email"])
        _set_user_cookie(response, request, user_id)
        utils.ensure_user_artifacts_dir(user_id)
        utils.ensure_user_skills_dir(user_id)
        utils.ensure_user_skills_list(user_id)
        try:
            utils.ensure_user_graph_dir(user_id)
            utils.ensure_user_wiki_dir(user_id)
            utils.ensure_user_documents_dir(user_id)
        except Exception:
            logger.exception("Failed to ensure graph dir for %s", user_id)
        _kick_graph_job(user_id)
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
        return _session_response(
            user_id,
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
        # Local bypass (localhost / ALLOW_LOCAL_AUTH_BYPASS) skips allowlist.
        user_id = local_user_id.strip()
        _set_user_cookie(response, request, user_id)
        utils.ensure_user_artifacts_dir(user_id)
        utils.ensure_user_skills_dir(user_id)
        utils.ensure_user_skills_list(user_id)
        try:
            utils.ensure_user_graph_dir(user_id)
            utils.ensure_user_wiki_dir(user_id)
            utils.ensure_user_documents_dir(user_id)
        except Exception:
            logger.exception("Failed to ensure graph dir for %s", user_id)
        _kick_graph_job(user_id)
        try:
            from application import task_store

            task_store.record_login(user_id, method="local")
        except Exception:
            logger.exception("Failed to record local login event")
        gateway_ready = _ensure_litellm_virtual_key(user_id)
        logger.warning(
            "Local auth bypass login: %s (llm_gateway_ready=%s)",
            user_id,
            gateway_ready,
        )
        return _session_response(user_id, llm_gateway_ready=gateway_ready)

    raise HTTPException(
        status_code=400, detail="credential, access_token, or user_id is required"
    )


@router.get("", response_model=SessionResponse | None)
def get_session(request: Request, response: Response) -> SessionResponse | None:
    raw_user = session_cookie.verify_session(
        request.cookies.get(SESSION_COOKIE) or ""
    )
    if (
        raw_user
        and not local_auth_bypass_enabled(request)
        and not allow_list.is_allowed_user(raw_user)
    ):
        # Drop stale cookie for accounts removed from the allow list.
        secure = _cookie_secure(request)
        response.delete_cookie(key=SESSION_COOKIE, samesite="lax", secure=secure)
        cloudfront_cookies.clear_signed_cookies(response, secure=secure)
        logger.warning("Cleared session for non-allow-listed user: %s", raw_user)
        return None

    user_id = get_optional_user_id(request)
    if not user_id:
        return None
    # Ensure workspace survives process restarts for an existing cookie session
    utils.ensure_user_artifacts_dir(user_id)
    utils.ensure_user_skills_dir(user_id)
    utils.ensure_user_skills_list(user_id)
    try:
        utils.ensure_user_graph_dir(user_id)
        utils.ensure_user_wiki_dir(user_id)
        utils.ensure_user_documents_dir(user_id)
    except Exception:
        logger.exception("Failed to ensure graph dir for %s", user_id)
    # Session restore (e.g. after server restart): start extract when fingerprint
    # is missing/stale. ensure_graph_job no-ops when source is unchanged.
    _kick_graph_job(user_id)
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
    return _session_response(user_id)


@router.patch("/settings", response_model=SessionResponse)
def patch_session_settings(
    body: SessionSettingsPatch, request: Request
) -> SessionResponse:
    """Update per-user feature settings (e.g. Knowledge Graph toggle)."""
    user_id = require_user_id(request)
    updates: dict[str, object] = {}
    if body.knowledge_graph_enabled is not None:
        updates["knowledge_graph_enabled"] = body.knowledge_graph_enabled
    if body.graph_pattern is not None:
        updates["graph_pattern"] = utils.normalize_graph_pattern(body.graph_pattern)
    prev_pattern = utils.get_graph_pattern(user_id)
    if updates:
        utils.save_user_settings(user_id, **updates)
    # Turning Knowledge Graph on force-runs extract (bypass unchanged/cooldown skips).
    if updates.get("knowledge_graph_enabled") is True:
        _kick_graph_job(user_id, force=True)
    new_pattern = updates.get("graph_pattern")
    if (
        isinstance(new_pattern, str)
        and new_pattern != prev_pattern
        and utils.is_knowledge_graph_enabled(user_id)
    ):
        try:
            from application.graph_jobs import republish_graph_html

            republish_graph_html(user_id, pattern=new_pattern)
        except Exception:
            logger.exception("Failed to republish graph HTML for %s", user_id)
    return _session_response(user_id)


@router.delete("", status_code=204)
def clear_session(request: Request, response: Response) -> None:
    secure = _cookie_secure(request)
    response.delete_cookie(
        key=SESSION_COOKIE,
        samesite="lax",
        secure=secure,
    )
    cloudfront_cookies.clear_signed_cookies(response, secure=secure)


@router.get("/google-drive", response_model=GoogleDriveStatusResponse)
def get_google_drive_status(request: Request) -> GoogleDriveStatusResponse:
    user_id = require_user_id(request)
    try:
        from application import google_drive_auth
    except ImportError:
        import google_drive_auth

    status = google_drive_auth.status_for_user(user_id)
    return GoogleDriveStatusResponse(**status)


@router.post("/google-drive", response_model=GoogleDriveStatusResponse)
def connect_google_drive(
    body: GoogleDriveConnectRequest, request: Request
) -> GoogleDriveStatusResponse:
    """Store a Google access token that includes Drive MCP scopes."""
    user_id = require_user_id(request)
    access_token = (body.access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    # Verify the token belongs to our OAuth client (rejects forged tokens).
    try:
        verify_google_access_token(access_token, _google_client_id())
    except ValueError as e:
        logger.warning("Google Drive connect rejected: %s", e)
        raise HTTPException(status_code=401, detail="Invalid Google access token") from e

    try:
        from application import google_drive_auth
    except ImportError:
        import google_drive_auth

    status = google_drive_auth.save_access_token(
        user_id,
        access_token,
        expires_in=body.expires_in,
        scope=body.scope,
        token_type=body.token_type,
    )
    return GoogleDriveStatusResponse(**status)


@router.delete("/google-drive", status_code=204)
def disconnect_google_drive(request: Request) -> None:
    user_id = require_user_id(request)
    try:
        from application import google_drive_auth
    except ImportError:
        import google_drive_auth

    google_drive_auth.clear_token(user_id)


class McpOAuthStatusResponse(BaseModel):
    connected: bool
    fresh: bool
    expires_at: float | None = None
    updated_at: str | None = None
    write: bool = False


class McpOAuthAuthorizeResponse(BaseModel):
    url: str
    # True when the provider only accepts loopback redirects and this deployment is not
    # loopback: the user must paste the post-approval address-bar URL to /exchange.
    manual: bool = False


class McpOAuthExchangeRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=4096)


# Hosted MCP servers connected through per-user OAuth (see mcp_oauth.py).
# Canva OAuth routes are not registered while canva MCP is disabled (see canva_auth.DISABLED).
_OAUTH_MCP_MODULES = {"notion": "notion_auth"}


def _oauth_module(name: str):
    import importlib

    module = _OAUTH_MCP_MODULES[name]
    try:
        return importlib.import_module(f"application.{module}")
    except ImportError:
        return importlib.import_module(module)


def _oauth_redirect_uri(request: Request, name: str) -> str:
    """Public callback URL (must be identical for authorize + token exchange)."""
    path = f"/api/session/{name}/callback"
    if not is_loopback_request(request):
        # Behind CloudFront -> ALB the Host header is not the viewer's domain;
        # the configured public URL is the origin the browser actually uses.
        sharing = (utils.load_config().get("sharing_url") or "").strip().rstrip("/")
        if sharing.startswith("https://"):
            return f"{sharing}{path}"
    secure = _cookie_secure(request)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    scheme = "https" if secure else "http"
    return f"{scheme}://{host}{path}"


def _register_oauth_mcp_routes(name: str) -> None:
    label = name.capitalize()

    def get_status(request: Request) -> McpOAuthStatusResponse:
        user_id = require_user_id(request)
        return McpOAuthStatusResponse(**_oauth_module(name).status_for_user(user_id))

    def authorize(request: Request, write: bool = False) -> McpOAuthAuthorizeResponse:
        """Return the OAuth URL; the browser opens it in a popup."""
        user_id = require_user_id(request)
        provider = _oauth_module(name).PROVIDER
        manual = bool(provider.loopback_only and not is_loopback_request(request))
        redirect_uri = (
            provider.manual_redirect_uri if manual else _oauth_redirect_uri(request, name)
        )
        try:
            url = _oauth_module(name).start(user_id, redirect_uri, write=write)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("%s authorize failed", label)
            raise HTTPException(
                status_code=502, detail=f"{label} OAuth start failed: {e}"
            ) from e
        return McpOAuthAuthorizeResponse(url=url, manual=manual)

    def exchange(
        body: McpOAuthExchangeRequest, request: Request
    ) -> McpOAuthStatusResponse:
        """Finish a manual flow from the URL the user pasted after approving."""
        user_id = require_user_id(request)
        auth = _oauth_module(name)
        try:
            state, code, iss = auth.oauth.parse_redirect(body.url)
            auth.oauth.finish(state, code, iss, expected_user_id=user_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.warning("%s manual exchange failed: %s", label, e)
            raise HTTPException(
                status_code=502, detail=f"{label} 연결에 실패했습니다. 다시 시도하세요."
            ) from e
        return McpOAuthStatusResponse(**auth.status_for_user(user_id))

    def callback(
        code: str | None = None,
        state: str | None = None,
        iss: str | None = None,
        error: str | None = None,
    ) -> Response:
        """OAuth redirect target. Identity comes from ``state`` (single-use, server-side)."""
        ok = False
        message = error or f"{label} connection failed"
        if not error and code and state:
            try:
                _oauth_module(name).finish(state, code, iss)
                ok = True
                message = "connected"
            except Exception as e:
                logger.warning("%s OAuth callback failed: %s", label, e)
                message = f"{label} 연결에 실패했습니다. 다시 시도하세요."
        payload = json.dumps({"type": f"{name}-oauth", "ok": ok, "message": message})
        title = f"{label} 연결됨" if ok else f"{label} 연결 실패"
        html = (
            "<!doctype html><meta charset=utf-8><title>%s</title>"
            "<body style=\"font-family:system-ui;padding:2rem\"><p>%s. 이 창은 닫아도 됩니다.</p>"
            "<script>try{window.opener&&window.opener.postMessage(%s,window.location.origin)}"
            "catch(e){}window.close()</script></body>"
        ) % (title, title, payload.replace("<", "\\u003c"))
        return Response(
            content=html, media_type="text/html", status_code=200 if ok else 400
        )

    def disconnect(request: Request) -> None:
        user_id = require_user_id(request)
        _oauth_module(name).clear_token(user_id)

    base = f"/{name}"
    router.add_api_route(
        base, get_status, methods=["GET"], response_model=McpOAuthStatusResponse
    )
    router.add_api_route(
        f"{base}/authorize",
        authorize,
        methods=["GET"],
        response_model=McpOAuthAuthorizeResponse,
    )
    router.add_api_route(
        f"{base}/exchange",
        exchange,
        methods=["POST"],
        response_model=McpOAuthStatusResponse,
    )
    router.add_api_route(f"{base}/callback", callback, methods=["GET"])
    router.add_api_route(base, disconnect, methods=["DELETE"], status_code=204)


for _oauth_name in _OAUTH_MCP_MODULES:
    _register_oauth_mcp_routes(_oauth_name)


def require_user_id(request: Request) -> str:
    user_id = get_optional_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="User session required")
    if local_auth_bypass_enabled(request):
        return user_id
    return allow_list.require_allowed_user(user_id)
