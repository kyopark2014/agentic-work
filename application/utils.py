import logging
import sys
import json
import boto3
import os

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

config = load_config()

bedrock_region = config['region']
projectName = config['projectName']
accountId = config['accountId']

