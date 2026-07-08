import json
import logging
import sys
import traceback
import chat
import utils
import httpx
import boto3
from datetime import datetime, timezone
from urllib.parse import urlparse
from botocore.auth import SigV4Auth as BotocoreSigV4Auth
from botocore.awsrequest import AWSRequest

from langchain_core.messages import HumanMessage, ToolMessage, AIMessageChunk, AIMessage
from bedrock_agentcore.runtime import BedrockAgentCoreApp

import bedrock_stream_usage_patch

bedrock_stream_usage_patch.apply_bedrock_stream_usage_patch()

logging.basicConfig(
    level=logging.INFO,  
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("agent")

# Monkey patch httpx.AsyncClient for SigV4 authentication
original_init = httpx.AsyncClient.__init__

def _sigv4_region_for_bedrock_agentcore_url(url: str) -> str:
    """Resolve AWS region for SigV4 signing from a bedrock-agentcore URL."""
    host = urlparse(url).netloc
    parts = host.split(".")
    try:
        idx = parts.index("bedrock-agentcore")
        if idx + 1 < len(parts) and parts[idx + 1] != "amazonaws":
            return parts[idx + 1]
    except ValueError:
        pass
    return utils.load_config().get("region", "us-west-2")

def patched_init(self, *args, **kwargs):
    # Add SigV4 signing event hook if needed
    async def sign_request(request: httpx.Request) -> None:
        """Sign the request with AWS SigV4 including the body"""
        url_str = str(request.url)
        # Only sign requests to bedrock-agentcore runtime endpoints in this region.
        if "bedrock-agentcore" not in url_str:
            return
        # Gateway MCP uses per-connection AgentCoreSigV4Auth (often us-east-1).
        if ".gateway.bedrock-agentcore." in url_str:
            return
        if request.headers.get("Authorization"):
            return
        
        # Get credentials
        boto_session = boto3.Session()
        credentials = boto_session.get_credentials().get_frozen_credentials()
        
        # Parse URL
        parsed_url = urlparse(url_str)
        host = parsed_url.netloc
        
        # Generate timestamp
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        
        # Read request body if available
        body = None
        if request.content:
            if isinstance(request.content, bytes):
                body = request.content
            else:
                try:
                    body = await request.aread()
                    if hasattr(request, '_content'):
                        request._content = body
                except Exception:
                    pass
        
        # Create AWS request headers
        aws_headers = {
            'host': host,
            'x-amz-date': timestamp,
            'Content-Type': request.headers.get('Content-Type', 'application/json'),
            'Accept': request.headers.get('Accept', 'application/json, text/event-stream')
        }
        
        if body:
            aws_headers['Content-Length'] = str(len(body))
        
        # Create AWS request for signing
        aws_request = AWSRequest(
            method=request.method,
            url=url_str,
            headers=aws_headers,
            data=body
        )
        
        # Sign the request
        region = _sigv4_region_for_bedrock_agentcore_url(url_str)
        auth = BotocoreSigV4Auth(credentials, "bedrock-agentcore", region)
        auth.add_auth(aws_request)
        
        # Update request headers
        request.headers['X-Amz-Date'] = timestamp
        request.headers['Authorization'] = aws_request.headers['Authorization']
        
        if credentials.token:
            request.headers['X-Amz-Security-Token'] = credentials.token
    
    # Add event_hooks to kwargs if not already present
    if 'event_hooks' not in kwargs:
        kwargs['event_hooks'] = {'request': [], 'response': []}
    elif not isinstance(kwargs['event_hooks'], dict):
        kwargs['event_hooks'] = {'request': [], 'response': []}
    
    if 'request' not in kwargs['event_hooks']:
        kwargs['event_hooks']['request'] = []
    
    # Add the sign_request hook
    kwargs['event_hooks']['request'].append(sign_request)

    # Call original init with modified kwargs
    original_init(self, *args, **kwargs)

auth_type = "iam"
        
app = BedrockAgentCoreApp()

@app.entrypoint
async def agent_langgraph(payload):
    """
    Invoke the agent with a payload
    """
    logger.info(f"payload: {payload}")
    query = payload.get("prompt")
    logger.info(f"query: {query}")

    mcp_servers = payload.get("mcp_servers", [])
    logger.info(f"mcp_servers: {mcp_servers}")

    skill_list = payload.get("skill_list", [])
    logger.info(f"skill_list: {skill_list}")

    model_name = payload.get("model_name")
    logger.info(f"model_name: {model_name}")

    user_id = payload.get("user_id")
    logger.info(f"user_id: {user_id}")

    chat.update(
        userId=user_id if user_id else chat.user_id,
        modelName=model_name if model_name else chat.model_name,
        debugMode=payload.get("debug_mode", chat.debug_mode),
        guardrailEnabled=payload.get("guardrail_enabled"),
    )

    history_mode = payload.get("history_mode", "Disable")
    logger.info(f"history_mode: {history_mode}")
    logger.info(f"guardrail_enabled: {chat.guardrail_enabled}")

    try:
        if auth_type == "iam":
            httpx.AsyncClient.__init__ = patched_init
            logger.info("Applied SigV4 monkey patch")

        if query and chat.guardrail_enabled and not chat.uses_converse_guardrail():
            blocked, blocked_message = chat.check_input_guardrail(query)
            if blocked:
                yield {
                    "result": {
                        "messages": [{"role": "assistant", "content": blocked_message}],
                        "image_url": [],
                    }
                }
                return

        try:
            app, config = await chat.create_agent(mcp_servers, skill_list, history_mode)
        except Exception as e:
            logger.error(f"Failed to create agent: {traceback.format_exc()}")
            yield {
                "result": {
                    "messages": [{"role": "assistant", "content": f"에이전트 초기화 오류: {e}"}],
                    "image_url": [],
                }
            }
            return
        if app is None:
            yield {"result": {"messages": [{"role": "assistant", "content": "사용 가능한 도구가 없습니다."}], "image_url": []}}
            return
        
        inputs = {
            "messages": [HumanMessage(content=query)]
        }

        result_text = ""
        tool_used = False
        tool_input_list = {}
        yielded_tool_ids = set()

        # call_model이 chain.astream을 쓰므로 LLM 토큰/청크가 그래프로 전달됨 (langgraph_agent.call_model)
        async for stream in app.astream(inputs, config, stream_mode="messages"):
            chunk = stream[0] if isinstance(stream, (list, tuple)) and stream else stream

            if isinstance(chunk, AIMessageChunk):
                content = chunk.content
                if isinstance(content, str) and content:
                    if tool_used:
                        result_text = content
                        tool_used = False
                    else:
                        result_text += content
                    yield {"data": content}
                elif isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            text_part = item.get("text", "")
                            if text_part:
                                if tool_used:
                                    result_text = text_part
                                    tool_used = False
                                else:
                                    result_text += text_part
                                yield {"data": text_part}
                        elif item.get("type") == "tool_use":
                            tool_use_id = item.get("id", "")
                            tool_name = item.get("name", "")
                            if tool_use_id and tool_name:
                                if tool_use_id not in tool_input_list:
                                    tool_input_list[tool_use_id] = ""
                            if "partial_json" in item:
                                pj = item.get("partial_json", "") or ""
                                if tool_use_id:
                                    tool_input_list[tool_use_id] = tool_input_list.get(tool_use_id, "") + pj
                                args_raw = tool_input_list.get(tool_use_id, "")
                                if tool_use_id and args_raw:
                                    try:
                                        args_obj = json.loads(args_raw)
                                        if tool_use_id not in yielded_tool_ids:
                                            yielded_tool_ids.add(tool_use_id)
                                            logger.info(
                                                f"tool_name: {tool_name}, content: {args_obj}, toolUseId: {tool_use_id}"
                                            )
                                            yield {
                                                "tool": tool_name,
                                                "input": args_obj,
                                                "toolUseId": tool_use_id,
                                            }
                                    except json.JSONDecodeError:
                                        pass
                if getattr(chunk, "tool_calls", None):
                    for tc in chunk.tool_calls:
                        if isinstance(tc, dict):
                            tid, name, args = (
                                tc.get("id", ""),
                                tc.get("name", ""),
                                tc.get("args", {}),
                            )
                        else:
                            tid = getattr(tc, "id", "") or ""
                            name = getattr(tc, "name", "") or ""
                            args = getattr(tc, "args", {}) or {}
                        if tid and tid not in yielded_tool_ids:
                            yielded_tool_ids.add(tid)
                            yield {"tool": name, "input": args, "toolUseId": tid}

            elif isinstance(chunk, AIMessage):
                content = chunk.content
                text_parts = []
                if isinstance(content, str) and content:
                    text_parts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_part = item.get("text", "")
                            if text_part:
                                text_parts.append(text_part)
                for text_part in text_parts:
                    if tool_used:
                        result_text = text_part
                        tool_used = False
                    else:
                        result_text += text_part
                    yield {"data": text_part}
                if getattr(chunk, "tool_calls", None):
                    for tc in chunk.tool_calls:
                        if isinstance(tc, dict):
                            tid, name, args = (
                                tc.get("id", ""),
                                tc.get("name", ""),
                                tc.get("args", {}),
                            )
                        else:
                            tid = getattr(tc, "id", "") or ""
                            name = getattr(tc, "name", "") or ""
                            args = getattr(tc, "args", {}) or {}
                        if tid and tid not in yielded_tool_ids:
                            yielded_tool_ids.add(tid)
                            yield {"tool": name, "input": args, "toolUseId": tid}

            elif isinstance(chunk, ToolMessage):
                logger.info(f"ToolMessage: {chunk.name}, {chunk.content}")
                tool_used = True
                yield {"toolResult": chunk.content, "toolUseId": chunk.tool_call_id}

        if not result_text.strip():
            result_text = "답변을 찾지 못하였습니다."

        final_output = {
            "messages": [{"role": "assistant", "content": result_text}],
            "image_url": [],
        }
        logger.info(f"final_output: {result_text[:200]!r}...")
        yield {"result": final_output}
    finally:
        if history_mode == "Enable":
            await chat.persist_checkpoint_to_session_storage()

if __name__ == "__main__":
    app.run()

