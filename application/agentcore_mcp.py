"""IAM (SigV4) access from ECS to an MCP server hosted on AgentCore Runtime (use-vault).

The ECS task role already allows ``InvokeAgentRuntime`` on ``runtime/use_vault_of_ob_note*``
(see ``ecs-task-agentcore-policy-for-*`` in installer.py) and ``ListAgentRuntimes``, so the
runtime ARN can be discovered by name when it is not configured.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any
from urllib.parse import quote

import httpx2

logger = logging.getLogger("agentcore_mcp")

VAULT_RUNTIME_NAMES = ("use_vault_of_ob_note", "use_vault_of_ob_docs")
_lock = threading.Lock()
_resolved: dict[str, tuple[str, str]] = {}  # key -> (url, region)


class SigV4Auth(httpx2.Auth):
    """Sign every request with the task role credentials (re-read per request)."""

    def __init__(self, region: str, service: str = "bedrock-agentcore"):
        self.region = region
        self.service = service

    def auth_flow(self, request: httpx2.Request):
        import boto3
        from botocore.auth import SigV4Auth as _Sign
        from botocore.awsrequest import AWSRequest

        credentials = boto3.Session().get_credentials().get_frozen_credentials()
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        _Sign(credentials, self.service, self.region).add_auth(aws_request)
        for key, value in aws_request.prepare().headers.items():
            request.headers[key] = value
        yield request


def _config() -> dict[str, Any]:
    try:
        from application import utils

        return utils.load_config() or {}
    except Exception:  # pragma: no cover - config is optional here
        return {}


def _runtime_url(arn: str, region: str) -> str:
    encoded = arn.replace(":", "%3A").replace("/", "%2F")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"


def _find_runtime_arn(region: str) -> str | None:
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    token = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = client.list_agent_runtimes(**kwargs)
        for item in page.get("agentRuntimes", []):
            name = item.get("agentRuntimeName", "")
            if any(name == n or name.startswith(n) for n in VAULT_RUNTIME_NAMES):
                return item.get("agentRuntimeArn")
        token = page.get("nextToken")
        if not token:
            return None


def use_vault_endpoint() -> tuple[str, str]:
    """``(streamable-http URL, region)`` of the use-vault MCP runtime (cached)."""
    with _lock:
        if "use-vault" in _resolved:
            return _resolved["use-vault"]
    cfg = _config()
    region = (
        cfg.get("use_vault_mcp_region") or cfg.get("region") or os.environ.get("AWS_REGION") or "us-west-2"
    )
    url = (cfg.get("use_vault_mcp_url") or os.environ.get("USE_VAULT_MCP_URL") or "").strip()
    if not url:
        arn = (cfg.get("use_vault_mcp_runtime_arn") or os.environ.get("USE_VAULT_MCP_RUNTIME_ARN") or "").strip()
        if not arn:
            arn = _find_runtime_arn(region) or ""
            if arn:
                logger.info("Discovered use-vault runtime: %s", arn)
        if not arn:
            raise ValueError("use-vault MCP runtime was not found (set use_vault_mcp_runtime_arn)")
        url = _runtime_url(arn, region)
    with _lock:
        _resolved["use-vault"] = (url, region)
    return url, region
