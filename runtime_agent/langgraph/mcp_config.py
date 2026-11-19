import logging
import sys
import utils
import os
import shutil
import boto3

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("mcp-config")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")

config = utils.load_config()
logger.info(f"config: {config}")

region = config["region"] if "region" in config else "us-west-2"
projectName = config["projectName"] if "projectName" in config else "mcp"
workingDir = os.path.dirname(os.path.abspath(__file__))
# 상위 디렉토리의 contents 폴더 경로 추가
parent_dir = os.path.dirname(workingDir)
contents_dir = os.path.join(parent_dir, "contents")
logger.info(f"workingDir: {workingDir}")
logger.info(f"contents_dir: {contents_dir}")

def get_agentcore_gateway_mcp_url(gateway_name: str, gateway_region: str) -> str | None:
    # Prefer URL already written by the root installer (avoids ListGateways on every request).
    configured_url = (
        config.get("agentcore_websearch_gateway_url")
        or os.environ.get("agentcore_websearch_gateway_url")
        or ""
    ).strip()
    if gateway_name == "gateway-websearch" and configured_url:
        logger.info(f"Using configured AgentCore gateway URL for {gateway_name}")
        return configured_url.rstrip("/")

    client = boto3.client("bedrock-agentcore-control", region_name=gateway_region)
    try:
        response = client.list_gateways()
        for item in response.get("items", []):
            if item.get("name") != gateway_name:
                continue

            gateway_id = item["gatewayId"]
            gateway = client.get_gateway(gatewayIdentifier=gateway_id)
            return gateway["gatewayUrl"].rstrip("/")
    except Exception as e:
        logger.error(f"Error resolving AgentCore gateway URL for {gateway_name}: {e}")

    return None


_VAULT_RUNTIME_NAMES = ("use_vault_of_ob_note", "use_vault_of_ob_docs")
_SEARCH_NOTE_RUNTIME_NAMES = ("search_note_of_ob_note",)


def _find_agent_runtime_arn(vault_region: str, names: tuple[str, ...]) -> str | None:
    """Discover an AgentCore Runtime ARN by exact name or name prefix."""
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=vault_region)
        token = None
        while True:
            kwargs: dict = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            page = client.list_agent_runtimes(**kwargs)
            for item in page.get("agentRuntimes", []):
                name = item.get("agentRuntimeName", "")
                if any(name == n or name.startswith(n) for n in names):
                    arn = (item.get("agentRuntimeArn") or "").strip()
                    if arn:
                        return arn
            token = page.get("nextToken")
            if not token:
                return None
    except Exception as e:
        logger.error(f"Error discovering AgentCore runtime {names}: {e}")
        return None


def _find_use_vault_runtime_arn(vault_region: str) -> str | None:
    """Discover the ob-note use-vault AgentCore Runtime ARN by name prefix."""
    return _find_agent_runtime_arn(vault_region, _VAULT_RUNTIME_NAMES)


def _find_search_note_runtime_arn(vault_region: str) -> str | None:
    """Discover the ob-note search-note AgentCore Runtime ARN by name prefix."""
    return _find_agent_runtime_arn(vault_region, _SEARCH_NOTE_RUNTIME_NAMES)


def _runtime_mcp_url_from_arn(arn: str, vault_region: str) -> str:
    encoded = arn.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{vault_region}.amazonaws.com/runtimes/"
        f"{encoded}/invocations?qualifier=DEFAULT"
    )


def get_use_vault_mcp_url() -> str | None:
    """Streamable-HTTP URL for ob-note use-vault AgentCore Runtime MCP (IAM SigV4).

    Resolution order: config/env URL → config/env ARN → ListAgentRuntimes discovery
    (same fallback as application/agentcore_mcp.py for ECS MCP Apps).
    """
    configured = (
        config.get("use_vault_mcp_url")
        or os.environ.get("use_vault_mcp_url")
        or os.environ.get("USE_VAULT_MCP_URL")
        or ""
    ).strip()
    if configured:
        return configured

    r = (
        config.get("use_vault_mcp_region")
        or config.get("region")
        or region
        or "us-west-2"
    )
    arn = (
        config.get("use_vault_mcp_runtime_arn")
        or os.environ.get("use_vault_mcp_runtime_arn")
        or os.environ.get("USE_VAULT_MCP_RUNTIME_ARN")
        or ""
    ).strip()
    if not arn:
        arn = _find_use_vault_runtime_arn(r) or ""
        if arn:
            logger.info(f"Discovered use-vault runtime: {arn}")
            # Keep process-local config in sync so later loads skip ListAgentRuntimes.
            config["use_vault_mcp_runtime_arn"] = arn
            config["use_vault_mcp_region"] = r
    if not arn:
        return None
    return _runtime_mcp_url_from_arn(arn, r)


def get_search_note_mcp_url() -> str | None:
    """Streamable-HTTP URL for ob-note search-note AgentCore Runtime MCP (IAM SigV4)."""
    configured = (
        config.get("search_note_mcp_url")
        or os.environ.get("search_note_mcp_url")
        or os.environ.get("SEARCH_NOTE_MCP_URL")
        or ""
    ).strip()
    if configured:
        return configured

    r = (
        config.get("search_note_mcp_region")
        or config.get("use_vault_mcp_region")
        or config.get("region")
        or region
        or "us-west-2"
    )
    arn = (
        config.get("search_note_mcp_runtime_arn")
        or os.environ.get("search_note_mcp_runtime_arn")
        or os.environ.get("SEARCH_NOTE_MCP_RUNTIME_ARN")
        or ""
    ).strip()
    if not arn:
        arn = _find_search_note_runtime_arn(r) or ""
        if arn:
            logger.info(f"Discovered search-note runtime: {arn}")
            config["search_note_mcp_runtime_arn"] = arn
            config["search_note_mcp_region"] = r
    if not arn:
        return None
    return _runtime_mcp_url_from_arn(arn, r)


def load_config(mcp_type):
    if mcp_type == "knowledge base":
        mcp_type = "kb-retriever"
    elif mcp_type == "aws documentation":
        mcp_type = "aws_documentation"    
    elif mcp_type == "trade info":
        mcp_type = "trade_info"
    elif mcp_type == "image generation":
        mcp_type = "image_generation"
    
    if mcp_type == "tavily":
        return {
            "mcpServers": {
                "tavily-search": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_tavily.py"
                    ]
                }
            }
        }

    elif mcp_type == "use-aws":
        return {
            "mcpServers": {
                "use-aws": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_use_aws.py"]
                }
            }
        }

    elif mcp_type == "aws_documentation":
        # Prefer preinstalled binary (pip / uv tool install) over uvx so
        # each agent init does not re-resolve and install 40+ packages.
        env = {"FASTMCP_LOG_LEVEL": "ERROR"}
        command = shutil.which("awslabs.aws-documentation-mcp-server")
        args: list[str] = []
        if not command:
            logger.warning(
                "awslabs.aws-documentation-mcp-server is not preinstalled; "
                "falling back to uvx (install once with: "
                "uv tool install awslabs.aws-documentation-mcp-server)"
            )
            command = "uvx"
            args = ["awslabs.aws-documentation-mcp-server"]
        return {
            "mcpServers": {
                "awslabs.aws-documentation-mcp-server": {
                    "command": command,
                    "args": args,
                    "env": env,
                }
            }
        }

    elif mcp_type == "kb-retriever":
        return {
            "mcpServers": {
                "kb-retriever": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_retrieve.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in chat.create_agent()
                    },
                }
            }
        }
    
    elif mcp_type == "trade_info":
        return {
            "mcpServers": {
                "trade-info": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_trade_info.py"
                    ]
                }
            }
        }    
    
    elif mcp_type == "web_fetch":
        # Prefer image-preinstalled package over npx (avoids NAT/npm cold start).
        command = shutil.which("mcp-server-fetch-typescript")
        args: list[str] = []
        if not command:
            local_entry = os.path.join(
                workingDir,
                "node_modules",
                "mcp-server-fetch-typescript",
                "build",
                "index.js",
            )
            if os.path.isfile(local_entry):
                command = "node"
                args = [local_entry]
            else:
                logger.warning(
                    "mcp-server-fetch-typescript is not preinstalled; "
                    "falling back to npx (slow on cold start). "
                    "Install with: npm install mcp-server-fetch-typescript"
                )
                command = "npx"
                args = ["-y", "mcp-server-fetch-typescript"]
        return {
            "mcpServers": {
                "web_fetch": {
                    "command": command,
                    "args": args,
                }
            }
        }
    
    elif mcp_type == "image_generation":
        return {
            "mcpServers": {
                "imageGeneration": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_image_generation.py"
                    ],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in chat.create_agent()
                    },
                }
            }
        }

    elif mcp_type == "notion":
        return {
            "mcpServers": {
                "notionApi": {
                    "command": "npx",
                    "args": ["-y", "@notionhq/notion-mcp-server"],
                    "env": {
                        "NOTION_TOKEN": utils.notion_api_key
                    }
                }
            }
        }

    elif mcp_type == "websearch":
        gateway_region = (
            config.get("agentcore_websearch_gateway_region")
            or os.environ.get("agentcore_websearch_gateway_region")
            or "us-west-2"
        )
        gateway_url = get_agentcore_gateway_mcp_url(
            "gateway-websearch", gateway_region
        )
        if not gateway_url:
            logger.info(
                "AgentCore gateway websearch MCP skipped: "
                f"gateway-websearch not found in {gateway_region}."
            )
            return {}
        return {
            "mcpServers": {
                "gateway-websearch": {
                    "type": "streamable_http",
                    "url": gateway_url,
                    "auth_type": "aws_sigv4",
                    "auth_region": gateway_region,
                    "auth_service": "bedrock-agentcore",
                }
            }
        }

    elif mcp_type == "use-vault":
        mcp_url = get_use_vault_mcp_url()
        if not mcp_url:
            logger.info(
                "use-vault MCP skipped: could not resolve use_vault_mcp_url "
                "(config/env ARN/URL missing and ListAgentRuntimes discovery failed)."
            )
            return {}
        auth_region = (
            config.get("use_vault_mcp_region")
            or config.get("region")
            or region
            or "us-west-2"
        )
        return {
            "mcpServers": {
                "use-vault": {
                    "type": "streamable_http",
                    "url": mcp_url,
                    "auth_type": "aws_sigv4",
                    "auth_region": auth_region,
                    "auth_service": "bedrock-agentcore",
                }
            }
        }

    elif mcp_type == "search-note":
        mcp_url = get_search_note_mcp_url()
        if not mcp_url:
            logger.info(
                "search-note MCP skipped: could not resolve search_note_mcp_url "
                "(config/env ARN/URL missing and ListAgentRuntimes discovery failed)."
            )
            return {}
        auth_region = (
            config.get("search_note_mcp_region")
            or config.get("use_vault_mcp_region")
            or config.get("region")
            or region
            or "us-west-2"
        )
        return {
            "mcpServers": {
                "search-note": {
                    "type": "streamable_http",
                    "url": mcp_url,
                    "auth_type": "aws_sigv4",
                    "auth_region": auth_region,
                    "auth_service": "bedrock-agentcore",
                }
            }
        }

    elif mcp_type == "noaa":
        return {
            "mcpServers": {
                "noaa-energy-news": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_noaa.py"],
                }
            }
        }

    elif mcp_type == "memory":
        return {
            "mcpServers": {
                "memory": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_memory.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in chat.create_agent()
                    },
                }
            }
        }

    elif mcp_type == "graph memory":
        return {
            "mcpServers": {
                "graph memory": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_graph_memory.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in chat.create_agent()
                    },
                }
            }
        }


    elif mcp_type == "wiki":
        return {
            "mcpServers": {
                "wiki": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_wiki.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in chat.create_agent()
                    },
                }
            }
        }

    elif mcp_type == "pubmed":
        return {
            "mcpServers": {
                "pubmed": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_pubmed.py"
                    ]
                }
            }
        }

    elif mcp_type == "chembl":
        return {
            "mcpServers": {
                "chembl": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_chembl.py"
                    ]
                }
            }
        }

    elif mcp_type == "clinicaltrial":
        return {
            "mcpServers": {
                "clinicaltrial": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_clinicaltrial.py"
                    ]
                }
            }
        }

    elif mcp_type == "arxiv-manual":
        return {
            "mcpServers": {
                "arxiv-manual": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_arxiv.py"
                    ]
                }
            }
        }

    elif mcp_type == "weather":
        return {
            "mcpServers": {
                "weather": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_weather.py"
                    ]
                }
            }
        }

    elif mcp_type == "canva":
        # Disabled: Canva DCR only allows localhost redirects (paste-back UX). See canva-mcp.md.
        logger.warning("canva MCP is disabled (localhost OAuth redirect constraint)")
        return None

    elif mcp_type in ("notion-remote", "notion remote", "notion_remote"):
        # Notion hosted MCP (OAuth bearer injected per request; see notion_bearer_auth.py).
        return {
            "mcpServers": {
                "notion-remote": {
                    "type": "streamable_http",
                    "url": "https://mcp.notion.com/mcp",
                    "auth_type": "notion_bearer",
                }
            }
        }

    elif mcp_type in ("drive", "google-drive", "google drive", "google_drive"):
        # Google remote Drive MCP (OAuth bearer injected at create_agent time).
        # https://developers.google.com/workspace/drive/api/guides/configure-mcp-server
        return {
            "mcpServers": {
                "drive": {
                    "type": "streamable_http",
                    "url": "https://drivemcp.googleapis.com/mcp/v1",
                    "auth_type": "google_drive_bearer",
                }
            }
        }

    elif mcp_type in ("excalidraw", "excalidraw-mcp", "excalidraw mcp"):
        # Open-source Excalidraw MCP Apps (no auth). Diagram UI via ui://excalidraw/...
        # https://mcp.excalidraw.com/mcp — Plus workspace MCP is a different endpoint.
        return {
            "mcpServers": {
                "excalidraw": {
                    "type": "streamable_http",
                    "url": "https://mcp.excalidraw.com/mcp",
                }
            }
        }

def load_selected_config(mcp_servers: dict):
    logger.info(f"mcp_servers: {mcp_servers}")
    
    loaded_config = {}
    for server in mcp_servers:
        config = load_config(server)
        if config:
            loaded_config.update(config["mcpServers"])
    return {
        "mcpServers": loaded_config
    }
