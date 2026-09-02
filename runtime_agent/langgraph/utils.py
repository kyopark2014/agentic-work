import logging
import sys
import json
import traceback
import boto3
import os
from pathlib import Path
from typing import Any
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

workingDir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(workingDir, "config.json")
# Prefer AgentCore /mnt/workspace; local fallback is .session_storage.
SESSION_STORAGE_DIR = os.environ.get(
    "SESSION_STORAGE_DIR",
    "/mnt/workspace"
    if os.path.isdir("/mnt/workspace")
    else os.path.join(workingDir, ".session_storage"),
)
SKILLS_DIR = os.path.join(workingDir, "skills")


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


def get_user_skills_list_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/skills.list (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "skills.list")


def get_user_graph_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/graph (does not create)."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph")


def get_user_wiki_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/wiki (does not create)."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "wiki")


def wiki_graphify_out_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_wiki_dir(user_id), "graphify-out")


def wiki_graph_json_path(user_id: str | None = None) -> str:
    return os.path.join(wiki_graphify_out_dir(user_id), "graph.json")


WIKI_SYNC_STATUS_FILENAME = ".wiki_sync_status.json"


def wiki_sync_status_path(user_id: str | None = None) -> str:
    return os.path.join(wiki_graphify_out_dir(user_id), WIKI_SYNC_STATUS_FILENAME)


def load_wiki_sync_status(user_id: str | None = None) -> dict[str, Any] | None:
    """Load mirrored ``.wiki_sync_status.json`` written by Application wiki_jobs."""
    path = wiki_sync_status_path(user_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def wiki_recall_blocked_message(
    user_id: str | None, graph_json_path: str | os.PathLike[str]
) -> str | None:
    """Return a user-facing error when ``recall_wiki`` should not run yet."""
    graph_json = Path(graph_json_path)
    status_doc = load_wiki_sync_status(user_id)
    if status_doc:
        st = str(status_doc.get("status") or "").strip().lower()
        if st in ("queued", "running"):
            msg = str(status_doc.get("message") or "").strip()
            base = msg or "Wiki 동기화가 진행 중입니다."
            return (
                f"{base} 완료 후(보통 1–3분) 다시 검색해 주세요. "
                "Settings → Wiki → Graph에서 진행 상태를 확인할 수 있습니다."
            )
        if st == "error" and not graph_json.is_file():
            err = str(
                status_doc.get("error") or status_doc.get("message") or "알 수 없는 오류"
            ).strip()
            return (
                f"Wiki 동기화에 실패했습니다: {err}. "
                "Settings → Wiki → Sync를 다시 실행하세요."
            )

    if not graph_json.is_file():
        return (
            "Wiki 그래프가 아직 없습니다. Settings → Wiki → Sync를 실행한 뒤 "
            "동기화가 완료되면 다시 검색하세요."
        )
    return None


def wiki_sources_path(user_id: str | None = None) -> str:
    return os.path.join(get_user_wiki_dir(user_id), "wiki_sources.json")


def get_wiki_source_folders(user_id: str | None = None) -> list[str]:
    """Configured Wiki Sync source folders (max 3). Empty → Sync uses raw/wiki root."""
    path = wiki_sources_path(user_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    folders = raw.get("AGENT_WIKI_SOURCES") if isinstance(raw, dict) else None
    if not isinstance(folders, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in folders:
        pth = str(item or "").strip()
        if not pth:
            continue
        abs_path = os.path.abspath(os.path.expanduser(pth))
        if abs_path in seen:
            continue
        seen.add(abs_path)
        out.append(abs_path)
        if len(out) >= 3:
            break
    return out



_DEFAULT_USER_SETTINGS: dict[str, object] = {
    "knowledge_graph_enabled": True,
}


def get_user_settings_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/settings.json (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "settings.json")


def load_user_settings(user_id: str | None) -> dict[str, object]:
    """Load per-user UI/feature settings. Missing file → defaults (KG on)."""
    settings = dict(_DEFAULT_USER_SETTINGS)
    path = get_user_settings_path(user_id)
    if not os.path.isfile(path):
        return settings
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and "knowledge_graph_enabled" in raw:
            settings["knowledge_graph_enabled"] = bool(raw["knowledge_graph_enabled"])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load user settings %s: %s", path, e)
    return settings


def is_knowledge_graph_enabled(user_id: str | None) -> bool:
    """True when Knowledge Graph feature is on (default)."""
    return bool(load_user_settings(user_id).get("knowledge_graph_enabled", True))


def is_hybrid_graph_search_enabled() -> bool:
    """True when config.json hybrid_graph_search is enable (embedding vector search)."""
    cfg = load_config() or {}
    raw = str(cfg.get("hybrid_graph_search") or "").strip().lower()
    return raw in {"enable", "enabled", "on", "true", "1", "yes"}


def list_skill_dir_names(skills_dir: str) -> list[str]:
    """Return subdirectory names that contain SKILL.md under skills_dir."""
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


def load_skills_list_file(path: str) -> list[str]:
    """Load skill names from a skills.list file (ignore blanks/comments)."""
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


def builtin_skill_names() -> list[str]:
    """Builtin skill names discovered by scanning SKILLS_DIR for SKILL.md."""
    return list_skill_dir_names(SKILLS_DIR)


def _merged_skill_names(user_id: str | None) -> list[str]:
    """Builtin skills/ dirs + per-user skill-creator skills (deduped, stable order)."""
    merged: list[str] = []
    seen: set[str] = set()
    for name in builtin_skill_names() + list_skill_dir_names(get_user_skills_dir(user_id)):
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def write_user_skills_list(user_id: str | None, names: list[str] | None = None) -> str:
    """Write {SESSION_STORAGE_DIR}/{user_id}/skills.list and return its path."""
    ensure_user_skills_dir(user_id)
    path = get_user_skills_list_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = names if names is not None else _merged_skill_names(user_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(merged) + ("\n" if merged else ""))
    logger.info("wrote user skills.list (%d skills) -> %s", len(merged), path)
    return path


def ensure_user_skills_list(user_id: str | None) -> str:
    """Sync {SESSION_STORAGE_DIR}/{user_id}/skills.list to builtin + user skills.

    Builtin names come from scanning ``skills/`` (SKILL.md dirs). User-created
    skills come from ``{user_id}/skills/``. Rewrite when the list drifts.
    """
    ensure_user_skills_dir(user_id)
    path = get_user_skills_list_path(user_id)
    desired = _merged_skill_names(user_id)
    existing = load_skills_list_file(path) if os.path.isfile(path) else []
    if existing == desired:
        logger.info(
            "user skills.list up to date (%d skills) -> %s",
            len(existing),
            path,
        )
        return path
    return write_user_skills_list(user_id, desired)


def update_user_skills_list(user_id: str | None) -> str:
    """Refresh {SESSION_STORAGE_DIR}/{user_id}/skills.list from skills dirs."""
    return write_user_skills_list(user_id)


def load_config():
    config = None

    try: 
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = {}

        session = boto3.Session()
        region = session.region_name
        config['region'] = region
        config['projectName'] = "power-trade"
        
        sts = boto3.client("sts")
        response = sts.get_caller_identity()
        accountId = response["Account"]
        config['accountId'] = accountId
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)    
    return config

config = load_config()

accountId = config.get('accountId')
if not accountId:
    sts = boto3.client("sts")
    response = sts.get_caller_identity()
    accountId = response["Account"]
    config['accountId'] = accountId
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

bedrock_region = config.get('region', 'us-west-2')
logger.info(f"bedrock_region: {bedrock_region}")
projectName = config.get('projectName', 'power-trade')
logger.info(f"projectName: {projectName}")

def get_contents_type(file_name):
    if file_name.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif file_name.lower().endswith((".pdf")):
        content_type = "application/pdf"
    elif file_name.lower().endswith((".txt")):
        content_type = "text/plain"
    elif file_name.lower().endswith((".csv")):
        content_type = "text/csv"
    elif file_name.lower().endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif file_name.lower().endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif file_name.lower().endswith((".xls")):
        content_type = "application/vnd.ms-excel"
    elif file_name.lower().endswith((".py")):
        content_type = "text/x-python"
    elif file_name.lower().endswith((".js")):
        content_type = "application/javascript"
    elif file_name.lower().endswith((".md")):
        content_type = "text/markdown"
    elif file_name.lower().endswith((".png")):
        content_type = "image/png"
    elif file_name.lower().endswith((".html", ".htm")):
        content_type = "text/html; charset=utf-8"
    else:
        content_type = "no info"    
    return content_type

# api key to use Tavily Search
def _load_tavily_api_key(app_config: dict) -> str:
    """Load Tavily API key from config.json or Secrets Manager."""
    key = app_config.get("tavily_api_key", "")
    if key:
        return key

    region = app_config.get("region", "us-west-2")
    # Shared secret name — matches installer.py / Runtime IAM (tavilyapikey*).
    secret_name = "tavilyapikey"
    secrets_client = boto3.client("secretsmanager", region_name=region)
    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        secret_data = json.loads(response["SecretString"])
        key = secret_data.get("tavily_api_key", "")
        if key:
            logger.info(f"tavily_key loaded from Secrets Manager: {secret_name}")
            return key
    except Exception as e:
        logger.debug(f"Could not load Tavily secret {secret_name}: {e}")
    return ""


tavily_key = _load_tavily_api_key(config)
if tavily_key:
    os.environ["TAVILY_API_KEY"] = tavily_key
    tavily_api_wrapper = TavilySearchAPIWrapper(tavily_api_key=tavily_key)
    logger.info("tavily_key is configured")
else:
    logger.info("tavily_key is not set.")


def _load_notion_api_key(app_config: dict) -> str:
    """Load Notion API key from config.json or Secrets Manager."""
    key = (app_config.get("notion_api_key") or "").strip()
    if key:
        return key

    region = app_config.get("region", "us-west-2")
    # Shared secret name — matches installer.py / Runtime IAM (notionapikey*).
    secret_name = "notionapikey"
    secrets_client = boto3.client("secretsmanager", region_name=region)
    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        secret_data = json.loads(response["SecretString"])
        key = (secret_data.get("notion_api_key") or "").strip()
        if key:
            logger.info(f"notion_api_key loaded from Secrets Manager: {secret_name}")
            return key
    except Exception as e:
        logger.debug(f"Could not load Notion secret {secret_name}: {e}")
    return ""


notion_api_key = _load_notion_api_key(config)
if notion_api_key:
    os.environ["NOTION_API_KEY"] = notion_api_key
    os.environ["NOTION_TOKEN"] = notion_api_key
    logger.info("notion_api_key is configured")
else:
    logger.info("notion_api_key is not set.")

