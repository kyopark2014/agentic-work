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
            # mcp 2.x removed mcp.server.fastmcp; pin 1.x for this server.
            args = [
                "--with",
                "mcp>=1.9.0,<2",
                "awslabs.aws-documentation-mcp-server",
            ]
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
        gateway_url = get_agentcore_gateway_mcp_url("gateway-websearch", "us-east-1")
        if not gateway_url:
            logger.info(
                "AgentCore gateway websearch MCP skipped: "
                "gateway-websearch not found in us-east-1."
            )
            return {}
        return {
            "mcpServers": {
                "gateway-websearch": {
                    "type": "streamable_http",
                    "url": gateway_url,
                    "auth_type": "aws_sigv4",
                    "auth_region": "us-east-1",
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
