import logging
import os

logger = logging.getLogger("runtime_mode")

BACKEND_MODE = "agentcore"


def backend_mode_label() -> str:
    return BACKEND_MODE


def use_agentcore_runtime() -> bool:
    """Agent inference always goes through AgentCore invoke_agent_runtime."""
    return True


def ensure_agentcore_backend() -> None:
    """Reject docker/local agent backend overrides at startup."""
    forced = os.environ.get("AGENT_BACKEND", "").strip().lower()
    if forced in {"docker", "local", "run_agent_in_docker"}:
        logger.warning(
            "AGENT_BACKEND=%s is ignored; backend always uses AgentCore runtime",
            forced,
        )

    use_docker = os.environ.get("USE_DOCKER_AGENT", "").strip().lower()
    if use_docker in {"1", "true", "yes"}:
        logger.warning(
            "USE_DOCKER_AGENT is ignored; backend always uses AgentCore runtime",
        )


def run_agent(
    prompt,
    user_id,
    mcp_servers,
    model_name,
    runtime_session_id,
    notification_queue=None,
    skill_list=None,
    guardrail_enabled=None,
    memory_enabled=None,
    llm_gateway_enabled=None,
    files=None,
):
    """Dispatch agent calls to AgentCore runtime only.

    If the user has a LiteLLM virtual key, LLM Gateway is enabled automatically.
    On missing key / errors, fall back to the caller toggle + shared config key.
    """
    from application import agentcore_client
    from application import utils as app_utils

    if not use_agentcore_runtime():
        raise RuntimeError("AgentCore runtime is required for agent execution")

    cfg = app_utils.load_config()
    gateway_url = (cfg.get("llm_gateway_url") or "").strip().rstrip("/")
    gateway_key = ""
    effective_enabled = bool(llm_gateway_enabled)

    try:
        try:
            from application import litellm_virtual_key
        except ImportError:
            import litellm_virtual_key

        user_key = litellm_virtual_key.get_cached_virtual_key(user_id)
        if not user_key and litellm_virtual_key.is_email_user_id(user_id):
            user_key = litellm_virtual_key.resolve_virtual_key_for_email(user_id)
        if user_key:
            gateway_key = user_key
            effective_enabled = True
            logger.info("LLM Gateway enabled via per-user virtual key for %s", user_id)
    except Exception as e:
        logger.warning(
            "Per-user LiteLLM virtual key unavailable; using existing gateway settings: %s",
            e,
        )

    if effective_enabled and not gateway_key:
        gateway_key = (cfg.get("llm_gateway_key") or "").strip()

    if effective_enabled and not (gateway_url and gateway_key):
        logger.warning(
            "LLM Gateway requested but url/key missing; falling back to Bedrock"
        )
        effective_enabled = False
        gateway_url = ""
        gateway_key = ""

    return agentcore_client.run_agent(
        prompt,
        user_id,
        mcp_servers,
        model_name,
        runtime_session_id,
        notification_queue=notification_queue,
        skill_list=skill_list,
        guardrail_enabled=guardrail_enabled,
        memory_enabled=memory_enabled,
        llm_gateway_enabled=effective_enabled,
        llm_gateway_url=gateway_url or None,
        llm_gateway_key=gateway_key or None,
        files=files,
    )
