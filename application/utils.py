import logging
import sys
import json
import traceback
import boto3
import os
from urllib import parse

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
favorite_tools_path = os.path.join(script_dir, "favorite_tools.json")
# Prefer shared S3 Files mount: AgentCore /mnt/workspace or ECS /mnt/app-data.
def _default_session_storage_dir() -> str:
    """Prefer the shared S3 Files mount used by AgentCore or ECS.

    Both mounts point at the same S3 Files root (``agentcore-sessions/``), so the Web UI
    and runtime see the same ``{user_id}/skills.list`` and ``{user_id}/skills/``.
    """
    for candidate in ("/mnt/workspace", "/mnt/app-data"):
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(script_dir, ".session_storage")


SESSION_STORAGE_DIR = os.environ.get("SESSION_STORAGE_DIR") or _default_session_storage_dir()


def sanitize_user_path_segment(user_id: str | None) -> str | None:
    """Return a safe single path segment for per-user workspace folders, or None."""
    if not user_id:
        return None
    # Collapse path separators so user_id cannot escape the intended prefix.
    segment = (
        str(user_id)
        .strip()
        .replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
    )
    return segment or None


def get_user_artifacts_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/artifacts (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "artifacts")


def ensure_user_artifacts_dir(user_id: str | None) -> str:
    """Create {SESSION_STORAGE_DIR}/{user_id}/artifacts if needed and return it."""
    artifacts_dir = get_user_artifacts_dir(user_id)
    os.makedirs(artifacts_dir, exist_ok=True)
    logger.info("user artifacts dir ready: %s", artifacts_dir)
    return artifacts_dir


def get_user_skills_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/skills (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "skills")


def ensure_user_skills_dir(user_id: str | None) -> str:
    """Create {SESSION_STORAGE_DIR}/{user_id}/skills if needed and return it."""
    skills_dir = get_user_skills_dir(user_id)
    os.makedirs(skills_dir, exist_ok=True)
    logger.info("user skills dir ready: %s", skills_dir)
    return skills_dir


def get_user_graph_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/graph (does not create)."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph")


def ensure_user_graph_dir(user_id: str | None) -> str:
    """Create session graph workspace: corpus/ + out/ (shared extract+publish).

    Returns the graph root: {SESSION_STORAGE_DIR}/{user_id}/graph
    """
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for graph path; expected a plain user id, "
            "not a signed session cookie"
        )
    graph_dir = os.path.join(SESSION_STORAGE_DIR, segment, "graph")
    for name in ("corpus", "out"):
        os.makedirs(os.path.join(graph_dir, name), exist_ok=True)
    logger.info("user graph dir ready: %s", graph_dir)
    return graph_dir


def user_graph_html_path(user_id: str | None) -> str:
    """Published HTML: {SESSION_STORAGE_DIR}/{user_id}/graph/out/graph.html"""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph", "out", "graph.html")


_DEFAULT_USER_SETTINGS: dict[str, bool] = {
    "knowledge_graph_enabled": True,
}


def get_user_settings_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/settings.json (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "settings.json")


def load_user_settings(user_id: str | None) -> dict[str, bool]:
    """Load per-user UI/feature settings. Missing file → defaults (KG on)."""
    settings = dict(_DEFAULT_USER_SETTINGS)
    path = get_user_settings_path(user_id)
    if not os.path.isfile(path):
        return settings
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "knowledge_graph_enabled" in raw:
                settings["knowledge_graph_enabled"] = bool(raw["knowledge_graph_enabled"])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load user settings %s: %s", path, e)
    return settings


def save_user_settings(user_id: str | None, **updates: bool) -> dict[str, bool]:
    """Merge updates into per-user settings.json and return the full settings."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for settings path; expected a plain user id, "
            "not a signed session cookie"
        )
    user_dir = os.path.join(SESSION_STORAGE_DIR, segment)
    os.makedirs(user_dir, exist_ok=True)
    settings = load_user_settings(user_id)
    for key, value in updates.items():
        if key in _DEFAULT_USER_SETTINGS:
            settings[key] = bool(value)
    path = get_user_settings_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("user settings saved: %s -> %s", path, settings)
    return settings


def is_knowledge_graph_enabled(user_id: str | None) -> bool:
    """True when Knowledge Graph feature is on (default)."""
    return bool(load_user_settings(user_id).get("knowledge_graph_enabled", True))


def get_user_skills_list_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/skills.list (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "skills.list")


def _list_skill_dir_names(skills_dir: str) -> list[str]:
    """Return subdirectory names that contain SKILL.md."""
    if not os.path.isdir(skills_dir):
        return []
    names: list[str] = []
    try:
        entries = sorted(os.listdir(skills_dir))
    except OSError as e:
        logger.warning("Failed to list skills directory %s: %s", skills_dir, e)
        return []
    for entry in entries:
        if os.path.isfile(os.path.join(skills_dir, entry, "SKILL.md")):
            names.append(entry)
    return names


def _load_skills_list_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("Failed to read skills.list %s: %s", path, e)
        return []


def _seed_skill_names(user_id: str | None) -> list[str]:
    """Builtin application/skills.list + skill-creator dirs under the user skills path."""
    default_path = os.path.join(script_dir, "skills.list")
    builtin = _load_skills_list_file(default_path)
    user_skills = _list_skill_dir_names(get_user_skills_dir(user_id))
    merged: list[str] = []
    seen: set[str] = set()
    for name in builtin + user_skills:
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def write_user_skills_list(user_id: str | None, names: list[str] | None = None) -> str:
    """Write {SESSION_STORAGE_DIR}/{user_id}/skills.list and return its path."""
    ensure_user_skills_dir(user_id)
    path = get_user_skills_list_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = names if names is not None else _seed_skill_names(user_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(merged) + ("\n" if merged else ""))
    logger.info(
        "wrote user skills.list (%d skills) -> %s",
        len(merged),
        path,
    )
    return path


def update_user_skills_list(user_id: str | None) -> str:
    """Rewrite per-user skills.list from application/skills.list + user skills dir.

    Prefer ``ensure_user_skills_list`` so a runtime-updated list is not wiped.
    """
    return write_user_skills_list(user_id)


def ensure_user_skills_list(user_id: str | None) -> str:
    """Use {SESSION_STORAGE_DIR}/{user_id}/skills.list; create it if missing.

    When the file already exists (e.g. written by AgentCore runtime on the shared
    S3 Files mount), keep it and only append newly discovered skill-creator dirs
    under ``{user_id}/skills/``. Seed from application/skills.list only when absent.
    """
    ensure_user_skills_dir(user_id)
    path = get_user_skills_list_path(user_id)
    if not os.path.isfile(path):
        return write_user_skills_list(user_id)

    existing = _load_skills_list_file(path)
    seen = set(existing)
    appended = [
        name
        for name in _list_skill_dir_names(get_user_skills_dir(user_id))
        if name not in seen
    ]
    if appended:
        return write_user_skills_list(user_id, existing + appended)
    logger.info(
        "using existing user skills.list (%d skills) -> %s",
        len(existing),
        path,
    )
    return path


def _account_id_from_config(config: dict) -> str | None:
    for value in config.values():
        if isinstance(value, str) and value.startswith("arn:aws:"):
            parts = value.split(":")
            if len(parts) > 4 and parts[4].isdigit():
                return parts[4]
    account_id = config.get("accountId")
    return account_id if isinstance(account_id, str) and account_id else None

def _fill_missing_config_defaults(config: dict) -> dict:
    if not config.get("projectName"):
        config["projectName"] = "agentcore"

    if not config.get("region"):
        gateway_region = config.get("agentcore_websearch_gateway_region")
        config["region"] = gateway_region if isinstance(gateway_region, str) and gateway_region else "us-west-2"

    if not config.get("accountId"):
        account_id = _account_id_from_config(config)
        if account_id:
            config["accountId"] = account_id
        else:
            try:
                session = boto3.Session()
                if not config.get("region"):
                    config["region"] = session.region_name or config["region"]
                sts = boto3.client("sts", region_name=config["region"])
                config["accountId"] = sts.get_caller_identity()["Account"]
            except Exception as e:
                logger.warning("Could not resolve accountId from AWS: %s", e)
                config.setdefault("accountId", "000000000000")
    return config

def load_config():
    config: dict = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("config.json must contain a JSON object")
        config = _fill_missing_config_defaults(loaded)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = _fill_missing_config_defaults({})
    return config


def load_favorite_tools() -> dict[str, list[str]]:
    fallback = {"MCP": [], "SKILL": []}
    try:
        with open(favorite_tools_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("favorite_tools.json not found: %s", favorite_tools_path)
        return fallback
    except Exception as e:
        logger.warning("Failed to load favorite_tools.json: %s", e)
        return fallback

    if not isinstance(data, dict):
        return fallback

    favorites: dict[str, list[str]] = {}
    for key in ("MCP", "SKILL"):
        values = data.get(key, [])
        if isinstance(values, list):
            favorites[key] = [v for v in values if isinstance(v, str) and v.strip()]
        else:
            favorites[key] = []
    return favorites


def get_initial_tool_defaults() -> tuple[list[str], list[str]]:
    favorite_tools = load_favorite_tools()
    default_skills = favorite_tools.get("SKILL") or []
    default_mcp_servers = favorite_tools.get("MCP") or []
    return default_skills, default_mcp_servers

config = load_config()

bedrock_region = config['region']
projectName = config['projectName']
accountId = config['accountId']
s3_bucket = config.get('s3_bucket')
s3_prefix = "docs"
s3_image_prefix = "images"
sharing_url = config.get('sharing_url', '')
knowledge_base_id = config.get('knowledge_base_id')
data_source_id = config.get('data_source_id')


def persist_config_updates(updates):
    """Merge values into in-memory config and write application/config.json."""
    global config
    if not updates:
        return
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        s = value.strip() if isinstance(value, str) else str(value)
        if not s:
            continue
        if config.get(key) != s:
            config[key] = s
            changed = True
    if not changed:
        return
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        logger.info(
            "Saved config.json updates: %s",
            ", ".join(str(k) for k in updates if updates.get(k)),
        )
    except Exception as e:
        logger.warning("Failed to write config.json: %s", e)


def get_llm_gateway_key() -> str:
    """Resolve LLM Gateway key: ECS-injected env first, then config.json."""
    env_key = (os.environ.get("LLM_GATEWAY_KEY") or "").strip()
    if env_key:
        return env_key
    return (load_config().get("llm_gateway_key") or "").strip()


def sync_llm_gateway_key_secret(key: str) -> None:
    """Best-effort upsert of `{project}/llm-gateway-key` for ECS secret injection."""
    value = (key or "").strip()
    if not value:
        return
    os.environ["LLM_GATEWAY_KEY"] = value
    try:
        from botocore.exceptions import ClientError

        cfg = load_config()
        project = (cfg.get("projectName") or "agentic-work").strip() or "agentic-work"
        region_name = (
            (cfg.get("region") or "").strip()
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-west-2"
        )
        secret_name = f"{project}/llm-gateway-key"
        client = boto3.client("secretsmanager", region_name=region_name)
        try:
            client.put_secret_value(SecretId=secret_name, SecretString=value)
            logger.info("Updated Secrets Manager secret: %s", secret_name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            client.create_secret(
                Name=secret_name,
                Description=f"LLM Gateway fallback API key for {project}",
                SecretString=value,
            )
            logger.info("Created Secrets Manager secret: %s", secret_name)
    except Exception as e:
        logger.debug("Could not sync LLM gateway key to Secrets Manager: %s", e)


def get_contents_type(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif lower.endswith(".png"):
        content_type = "image/png"
    elif lower.endswith(".webp"):
        content_type = "image/webp"
    elif lower.endswith(".gif"):
        content_type = "image/gif"
    elif lower.endswith(".pdf"):
        content_type = "application/pdf"
    elif lower.endswith(".txt"):
        content_type = "text/plain"
    elif lower.endswith(".csv"):
        content_type = "text/csv"
    elif lower.endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif lower.endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif lower.endswith(".xls"):
        content_type = "application/vnd.ms-excel"
    elif lower.endswith(".py"):
        content_type = "text/x-python"
    elif lower.endswith(".js"):
        content_type = "application/javascript"
    elif lower.endswith(".md"):
        content_type = "text/markdown"
    else:
        content_type = "no info"
    return content_type


def _sanitize_s3_user_segment(user_id: str | None) -> str | None:
    """Return a safe single path segment for per-user S3 folders, or None."""
    return sanitize_user_path_segment(user_id)


def upload_to_s3(
    file_bytes: bytes,
    file_name: str,
    user_id: str | None = None,
) -> dict | None:
    """Upload a file to S3 under docs/ (or images/) and return upload metadata.

    When ``user_id`` is provided, the object key becomes
    ``{prefix}/{user_id}/{file_name}`` so each user has a separate folder.
    """
    if not s3_bucket:
        logger.error("s3_bucket is not configured")
        return None

    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        content_type = get_contents_type(file_name)
        logger.info("content_type: %s", content_type)

        if content_type.startswith("image/"):
            prefix = s3_image_prefix
        else:
            prefix = s3_prefix

        user_segment = _sanitize_s3_user_segment(user_id)
        if user_segment:
            s3_key = f"{prefix}/{user_segment}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(user_segment)}/{parse.quote(file_name)}"
        else:
            s3_key = f"{prefix}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(file_name)}"
        user_meta = {"content_type": content_type}

        put_params = {
            "Bucket": s3_bucket,
            "Key": s3_key,
            "Metadata": user_meta,
            "Body": file_bytes,
        }
        if content_type != "no info":
            put_params["ContentType"] = content_type
        if content_type == "application/pdf":
            put_params["ContentDisposition"] = "inline"

        response = s3_client.put_object(**put_params)
        logger.info("upload response: %s", response)

        url = None
        if sharing_url:
            url = f"{sharing_url.rstrip('/')}/{relative_url_path}"

        return {
            "file_name": file_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "url": url,
        }
    except Exception:
        logger.error("Error uploading to S3: %s", traceback.format_exc())
        return None


ACTIVE_INGESTION_STATUSES = ("STARTING", "IN_PROGRESS")


def _bedrock_agent_client():
    return boto3.client(
        service_name="bedrock-agent",
        region_name=bedrock_region,
    )


def get_active_ingestion_job() -> dict | None:
    """Return an in-flight ingestion job if Knowledge Base sync is already running."""
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None

    try:
        bedrock_client = _bedrock_agent_client()
        for status in ACTIVE_INGESTION_STATUSES:
            response = bedrock_client.list_ingestion_jobs(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                filters=[
                    {
                        "attribute": "STATUS",
                        "operator": "EQ",
                        "values": [status],
                    }
                ],
                maxResults=1,
                sortBy={
                    "attribute": "STARTED_AT",
                    "order": "DESCENDING",
                },
            )
            summaries = response.get("ingestionJobSummaries") or []
            if not summaries:
                continue
            job = summaries[0]
            logger.info("Active ingestion job found: %s", job)
            return {
                "ingestion_job_id": job.get("ingestionJobId"),
                "status": job.get("status"),
                "started_at": str(job["startedAt"]) if job.get("startedAt") else None,
            }
        return None
    except Exception:
        logger.error("Error listing ingestion jobs: %s", traceback.format_exc())
        raise


def sync_data_source() -> dict | None:
    """Start a Knowledge Base ingestion job for the configured data source."""
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None

    try:
        bedrock_client = _bedrock_agent_client()
        response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
        )
        logger.info("start_ingestion_job response: %s", response)
        job = response.get("ingestionJob", {})
        return {
            "ingestion_job_id": job.get("ingestionJobId"),
            "status": job.get("status"),
        }
    except Exception:
        logger.error("Error syncing data source: %s", traceback.format_exc())
        return None

