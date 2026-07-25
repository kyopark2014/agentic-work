import logging
import os
import json

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

try:
    from application import utils
    from application.api.routes_auth import get_optional_user_id, local_auth_bypass_enabled
    from application.api.routes_admin import is_admin_user, require_admin
    from application.llm_gateway_models import ui_models_for_gateway_ids
except ImportError:
    import utils
    from routes_auth import get_optional_user_id, local_auth_bypass_enabled  # type: ignore
    from routes_admin import is_admin_user, require_admin  # type: ignore
    from llm_gateway_models import ui_models_for_gateway_ids  # type: ignore

logger = logging.getLogger("routes_config")

router = APIRouter(prefix="/api/config", tags=["config"])

_APPLICATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME_CONFIG_PATH = os.path.normpath(
    os.path.join(_APPLICATION_DIR, "..", "runtime_agent", "langgraph", "config.json")
)

MODELS = [
    "Claude 5.0 Sonnet",
    "Claude 5.0 Opus",
    "Claude 4.6 Sonnet",
    "Claude Fable 5",
    "Claude 4.8 Opus",
    "Claude 4.7 Opus",
    "Claude 4.6 Opus",
    "Claude 4.5 Opus",
    "Claude 4.5 Sonnet",
    "Claude 4.5 Haiku",
    "OpenAI GPT 5.4",
    "OpenAI GPT 5.5",
    "OpenAI GPT 5.6 Sol",
    "OpenAI GPT 5.6 Terra",
    "OpenAI GPT 5.6 Luna",
    "OpenAI OSS 120B",
    "OpenAI OSS 20B",
]

DEFAULT_MODEL = "Claude 4.6 Sonnet"
DEFAULT_GATEWAY_MODEL = "Claude 4.6 Sonnet"


def load_capability_list(filename: str) -> list[str]:
    path = os.path.join(_APPLICATION_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        logger.warning("Capability list not found: %s", path)
        return []


class DefaultsPatch(BaseModel):
    default_skills: list[str] | None = None
    default_mcp_servers: list[str] | None = None


class LlmGatewaySettings(BaseModel):
    url: str = ""
    # Empty string means "keep existing stored key" (never echoed to clients).
    key: str = ""


def _llm_gateway_from_config() -> tuple[str, str]:
    cfg = utils.load_config()
    url = (cfg.get("llm_gateway_url") or "").strip().rstrip("/")
    key = utils.get_llm_gateway_key()
    return url, key


def _save_llm_gateway(url: str, key: str | None = None) -> None:
    """Persist gateway URL; update key only when a non-empty value is provided."""
    updates: dict[str, str] = {
        "llm_gateway_url": url.rstrip("/"),
    }
    if key:
        updates["llm_gateway_key"] = key
    utils.persist_config_updates(updates)
    if key:
        utils.sync_llm_gateway_key_secret(key)
    # Keep AgentCore runtime config in sync for image rebuilds / local runs.
    try:
        if os.path.isfile(_RUNTIME_CONFIG_PATH):
            with open(_RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                runtime_cfg = json.load(f)
            if not isinstance(runtime_cfg, dict):
                runtime_cfg = {}
            runtime_cfg.update(updates)
            with open(_RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(runtime_cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
            logger.info("Synced llm gateway settings to runtime config.json")
    except Exception as exc:
        logger.warning("Failed to sync runtime config.json: %s", exc)


def _probe_llm_gateway(url: str, key: str, *, timeout: float = 15.0) -> dict:
    models_url = f"{url.rstrip('/')}/v1/models"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                models_url,
                headers={"Authorization": f"Bearer {key}"},
            )
        if response.status_code >= 400:
            logger.warning(
                "LLM Gateway verify failed: status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return {
                "ok": False,
                "message": f"모델 확인 실패 (HTTP {response.status_code})",
                "models": [],
                "ui_models": [],
            }
        payload = response.json()
        models = [
            item.get("id")
            for item in (payload.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        ui_models = ui_models_for_gateway_ids(models, preferred_order=MODELS)
        return {
            "ok": True,
            "message": (
                f"모델 {len(ui_models)}개 확인됨"
                if ui_models
                else f"등록 모델 {len(models)}개 (UI 매핑 없음)"
            ),
            "models": models,
            "ui_models": ui_models,
        }
    except Exception as exc:
        logger.exception("LLM Gateway verify error")
        return {
            "ok": False,
            "message": f"모델 확인 요청 실패: {exc}",
            "models": [],
            "ui_models": [],
        }


def _gateway_ui_models() -> list[str]:
    """Resolve UI model list from live gateway, or mapped catalog fallback."""
    url, key = _llm_gateway_from_config()
    if not url or not key:
        return []

    result = _probe_llm_gateway(url, key, timeout=5.0)
    if result.get("ok") and result.get("ui_models"):
        return result["ui_models"]

    return ui_models_for_gateway_ids(None, preferred_order=MODELS)


@router.get("")
def get_config(request: Request):
    skill_options = load_capability_list("skills.list")
    mcp_options = load_capability_list("mcp.list")
    default_skills, default_mcp = utils.get_initial_tool_defaults()
    default_skills = [s for s in default_skills if s in skill_options]
    default_mcp = [m for m in default_mcp if m in mcp_options]
    if not default_skills and "skill-creator" in skill_options:
        default_skills = ["skill-creator"]
    if not default_mcp:
        logger.info("No initial MCP defaults matched current capability list")
    config = utils.load_config()
    gateway_url, gateway_key = _llm_gateway_from_config()
    gateway_models = _gateway_ui_models()
    default_gateway_model = (
        DEFAULT_GATEWAY_MODEL
        if DEFAULT_GATEWAY_MODEL in gateway_models
        else (gateway_models[0] if gateway_models else DEFAULT_MODEL)
    )
    session_user = get_optional_user_id(request)
    return {
        "projectName": config.get("projectName", "agent"),
        "google_client_id": (config.get("google_client_id") or "").strip(),
        "local_auth_bypass": local_auth_bypass_enabled(request),
        "is_admin": bool(session_user and is_admin_user(session_user)),
        "skills": skill_options,
        "mcp_servers": mcp_options,
        "models": MODELS,
        "gateway_models": gateway_models,
        "default_model": DEFAULT_MODEL,
        "default_gateway_model": default_gateway_model,
        "default_skills": default_skills,
        "default_mcp_servers": default_mcp,
        "llm_gateway_configured": bool(gateway_url and gateway_key),
    }


@router.get("/llm-gateway")
def get_llm_gateway(_admin: str = Depends(require_admin)):
    """Admin-only. Never returns the secret key to the browser."""
    url, key = _llm_gateway_from_config()
    return {
        "url": url,
        "configured": bool(url and key),
        "key_configured": bool(key),
    }


@router.post("/llm-gateway/verify")
def verify_llm_gateway(
    body: LlmGatewaySettings | None = None,
    _admin: str = Depends(require_admin),
):
    """Admin-only. Probe LiteLLM /v1/models; save on success.

    Empty ``key`` in the body keeps the existing stored / env key.
    The key is never included in the response.
    """
    stored_url, stored_key = _llm_gateway_from_config()
    if body is not None:
        url = (body.url or "").strip().rstrip("/") or stored_url
        submitted_key = (body.key or "").strip()
        key = submitted_key or stored_key
        key_provided = bool(submitted_key)
    else:
        url, key = stored_url, stored_key
        key_provided = False

    if not url or not key:
        return {
            "ok": False,
            "message": "url과 key가 모두 필요합니다. (Key는 비워두면 기존 값을 사용합니다)",
            "models": [],
            "ui_models": [],
        }

    result = _probe_llm_gateway(url, key)
    if result.get("ok"):
        _save_llm_gateway(url, key if key_provided else None)
    return result


@router.patch("/defaults")
def patch_defaults(
    body: DefaultsPatch,
    _admin: str = Depends(require_admin),
):
    utils.save_favorite_tools(
        skills=body.default_skills,
        mcp_servers=body.default_mcp_servers,
    )
    return {"ok": True}
