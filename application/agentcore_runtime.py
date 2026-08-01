import boto3
from botocore.exceptions import ClientError, ConnectionError as BotocoreConnectionError
from botocore.exceptions import EndpointConnectionError, ProxyConnectionError
import logging
import sys
import uuid

try:
    from application import utils
except ImportError:
    import utils

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("agentcore_runtime")

config = utils.load_config()

bedrock_region = config['region']
accountId = config['accountId']
projectName = config['projectName']


def _runtime_id_from_arn(arn: str) -> str:
    """Extract agentRuntimeId from an AgentCore runtime ARN."""
    return arn.rsplit("/", 1)[-1] if arn else ""


def _candidate_runtime_names(agent_name: str, agent_type: str | None) -> list:
    """Candidate AgentCore runtime names, including installer naming (e.g. strands_runtime)."""
    names = [agent_name]
    project_slug = projectName.replace("-", "_")
    names.append(project_slug)
    names.append(f"runtime_{project_slug}")
    if agent_type:
        normalized = agent_type.replace("-", "_")
        names.append(f"agent_runtime_{normalized}")
        names.append(normalized)
        names.append(f"runtime_{normalized}")
        names.append(f"{project_slug}_{normalized}")
    seen = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def _agentcore_control_client():
    # Control-plane SDK service id (distinct from data-plane "bedrock-agentcore").
    # IAM actions still use the "bedrock-agentcore:" prefix.
    return boto3.client("bedrock-agentcore-control", region_name=bedrock_region)


def _list_all_agent_runtimes(client) -> list:
    """Consume all list_agent_runtimes pages via nextToken."""
    runtimes: list = []
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_agent_runtimes(**kwargs)
        runtimes.extend(response.get("agentRuntimes") or [])
        next_token = response.get("nextToken")
        if not next_token:
            break
    return runtimes


def _lookup_runtime_by_name(agent_name: str, agent_type: str | None) -> str | None:
    """Find a READY AgentCore runtime ARN by candidate name."""
    candidate_names = _candidate_runtime_names(agent_name, agent_type)
    client = _agentcore_control_client()
    try:
        runtimes = _list_all_agent_runtimes(client)
    except (ClientError, BotocoreConnectionError, EndpointConnectionError, ProxyConnectionError) as exc:
        logger.warning("Failed to list agent runtimes: %s", exc)
        return None
    logger.info(f"Looking up agent runtime in {len(runtimes)} runtimes")
    logger.info(f"Candidate runtime names: {candidate_names}")

    for agent_runtime in runtimes:
        if agent_runtime.get("agentRuntimeName") in candidate_names:
            arn = agent_runtime.get("agentRuntimeArn")
            logger.info(f"Matched runtime '{agent_runtime.get('agentRuntimeName')}': {arn}")
            return arn

    logger.error(f"No agent runtime matched candidates: {candidate_names}")
    return None


def _validation_unavailable(exc: Exception) -> bool:
    if isinstance(exc, (ProxyConnectionError, EndpointConnectionError, BotocoreConnectionError)):
        return True
    message = str(exc).lower()
    return "proxy" in message or "could not connect" in message or "connection" in message


def _is_runtime_arn_valid(arn: str) -> bool:
    """Return True if the AgentCore runtime ARN still exists."""
    runtime_id = _runtime_id_from_arn(arn)
    if not runtime_id:
        return False

    client = _agentcore_control_client()
    try:
        client.get_agent_runtime(agentRuntimeId=runtime_id)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ValidationException"):
            return False
        raise


def load_agentcore_config(agent_name, agent_type=None):
    """Resolve AgentCore runtime ARN from config or Bedrock control plane."""
    configured_arns = []
    direct_arn = config.get("agent_runtime_arn")
    if direct_arn:
        configured_arns.append(("agent_runtime_arn", direct_arn))
    if agent_type:
        typed_arn = config.get(f"agent_runtime_arn_{agent_type}")
        if typed_arn and typed_arn not in {arn for _, arn in configured_arns}:
            configured_arns.append((f"agent_runtime_arn_{agent_type}", typed_arn))

    for key, arn in configured_arns:
        try:
            if _is_runtime_arn_valid(arn):
                logger.info(f"Using {key} from config: {arn}")
                return arn
        except Exception as exc:
            if _validation_unavailable(exc):
                logger.warning(
                    "Runtime ARN validation unavailable (%s); using %s from config: %s",
                    exc,
                    key,
                    arn,
                )
                return arn
            raise
        logger.warning(
            f"Configured {key} is missing or deleted; falling back to runtime name lookup: {arn}"
        )

    try:
        return _lookup_runtime_by_name(agent_name, agent_type)
    except Exception as exc:
        if configured_arns and _validation_unavailable(exc):
            key, arn = configured_arns[0]
            logger.warning(
                "Runtime lookup unavailable (%s); using %s from config: %s",
                exc,
                key,
                arn,
            )
            return arn
        raise


def runtime_session_id_for(user_id: str, history_mode: str) -> str:
    """AgentCore runtimeSessionId (min length 33).

    Chat mode: deterministic per user_id so history survives client restarts.
    Agent mode: ephemeral session per request.
    """
    if history_mode == "Enable" and user_id:
        seed = f"agentcore-session-{user_id}"
        session_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    else:
        session_id = str(uuid.uuid4())
    logger.info(f"runtime_session_id: {session_id} (history_mode={history_mode})")
    return session_id
