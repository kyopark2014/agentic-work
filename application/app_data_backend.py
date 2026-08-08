"""Shared application data backend: S3 Files mount, or direct S3 when local.

ECS mounts a dedicated S3 Files filesystem at /mnt/app-data (prefix app-data/).
Runtime mounts agentcore-sessions/ at /mnt/workspace separately.
Locally that mount is usually absent; when config.json has s3_bucket we talk to
the same objects via the S3 API so local runs share tasks.db / virtual_key.json
with production.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("app_data_backend")

_APPLICATION_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_WORKING_DIR = os.path.join(_APPLICATION_DIR, "data")
_DEFAULT_MOUNT = "/mnt/app-data"
S3_FILES_PREFIX = "app-data/"


def working_dir() -> str:
    custom = (os.environ.get("APP_DATA_DIR") or "").strip()
    if custom:
        return custom
    return _DEFAULT_WORKING_DIR


def mount_dir() -> str:
    return (os.environ.get("TASK_DB_MOUNT") or os.environ.get("APP_DATA_MOUNT") or _DEFAULT_MOUNT).strip() or _DEFAULT_MOUNT


def mount_available() -> bool:
    path = mount_dir()
    return os.path.isdir(path) and os.access(path, os.W_OK)


def _load_config() -> dict:
    try:
        try:
            from application import utils
        except ImportError:
            import utils

        cfg = utils.load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.debug("Could not load config for app data backend: %s", e)
        return {}


def project_name() -> str:
    env_name = (os.environ.get("TASK_DB_PROJECT") or "").strip()
    if env_name:
        return env_name
    cfg = _load_config()
    name = cfg.get("projectName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "agentic-work"


def s3_bucket_and_region() -> tuple[Optional[str], str]:
    env_bucket = (os.environ.get("APP_DATA_S3_BUCKET") or "").strip()
    cfg = _load_config()
    bucket = env_bucket or (cfg.get("s3_bucket") or "").strip() or None
    region = (cfg.get("region") or os.environ.get("AWS_REGION") or "us-west-2").strip()
    return bucket, region


def s3_available() -> bool:
    """True when we can use the S3 API as a stand-in for the S3 Files mount."""
    if mount_available():
        return False
    bucket, _ = s3_bucket_and_region()
    if not bucket:
        return False
    # Allow explicit disable for offline local work.
    if (os.environ.get("APP_DATA_S3_DISABLE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    return True


def backend_mode() -> str:
    """Return 'mount' | 's3' | 'local'."""
    if mount_available():
        return "mount"
    if s3_available():
        return "s3"
    return "local"


def s3_key(*parts: str) -> str:
    """Build an object key under app-data/."""
    cleaned = [p.strip("/").replace("\\", "/") for p in parts if p and str(p).strip()]
    return S3_FILES_PREFIX + "/".join(cleaned)


def tasks_db_s3_key() -> str:
    return s3_key("application-database", project_name(), "tasks.db")


def virtual_key_s3_key() -> str:
    return s3_key("litellm", "virtual_key.json")


def persistent_tasks_db_path() -> str:
    """Path on the S3 Files mount (ECS). Unused for pure S3/local backends."""
    return os.path.join(mount_dir(), "application-database", project_name(), "tasks.db")


def persistent_virtual_key_path() -> str:
    return os.path.join(mount_dir(), "litellm", "virtual_key.json")


def working_tasks_db_path() -> str:
    custom = (os.environ.get("TASK_DB_WORKING_PATH") or "").strip()
    if custom:
        return custom
    return os.path.join(working_dir(), "tasks.db")


def working_virtual_key_path() -> str:
    return os.path.join(working_dir(), "litellm", "virtual_key.json")


def _s3_client(region: str):
    return boto3.client("s3", region_name=region)


def s3_object_exists(bucket: str, key: str, region: str) -> bool:
    try:
        _s3_client(region).head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def download_s3_file(bucket: str, key: str, destination: str, region: str) -> bool:
    """Download S3 object to destination. Returns False if object is missing."""
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    client = _s3_client(region)
    try:
        client.download_file(bucket, key, destination)
        logger.info("Downloaded s3://%s/%s -> %s", bucket, key, destination)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            logger.info("S3 object not found: s3://%s/%s", bucket, key)
            return False
        raise


def upload_s3_file(bucket: str, key: str, source: str, region: str) -> None:
    if not os.path.isfile(source) or os.path.getsize(source) <= 0:
        raise FileNotFoundError(f"Cannot upload missing/empty file: {source}")
    client = _s3_client(region)
    client.upload_file(source, bucket, key)
    logger.info("Uploaded %s -> s3://%s/%s", source, bucket, key)


def download_s3_json(bucket: str, key: str, region: str) -> Optional[dict]:
    client = _s3_client(region)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read().decode("utf-8")
        data = json.loads(body)
        if isinstance(data, dict):
            return data
        logger.warning("S3 JSON at s3://%s/%s is not an object; ignoring", bucket, key)
        return None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON at s3://%s/%s: %s", bucket, key, e)
        return None


def upload_s3_json(bucket: str, key: str, data: dict, region: str) -> None:
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    client = _s3_client(region)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("Uploaded JSON -> s3://%s/%s", bucket, key)
