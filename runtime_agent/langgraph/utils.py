import logging
import sys
import json
import traceback
import boto3
import os
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
WORKSPACE_DIR = os.path.join(workingDir, "workspace")


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
    """Absolute path to workspace/{user_id}/artifacts (does not create the directory)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(WORKSPACE_DIR, segment, "artifacts")


def ensure_user_artifacts_dir(user_id: str | None) -> str:
    """Create workspace/{user_id}/artifacts if needed and return its absolute path."""
    artifacts_dir = get_user_artifacts_dir(user_id)
    os.makedirs(artifacts_dir, exist_ok=True)
    logger.info("user artifacts dir ready: %s", artifacts_dir)
    return artifacts_dir


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

