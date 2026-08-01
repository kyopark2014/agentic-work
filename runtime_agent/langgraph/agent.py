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
from opentelemetry.instrumentation.langchain import LangchainInstrumentor

import bedrock_stream_usage_patch

bedrock_stream_usage_patch.apply_bedrock_stream_usage_patch()
LangchainInstrumentor().instrument()

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

    files = payload.get("files") or []
    if not isinstance(files, list):
        files = [files] if files else []
    files = [str(f).strip() for f in files if str(f).strip()]
    logger.info(f"files: {files}")

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
        memoryEnabled=payload.get("memory_enabled"),
        llmGatewayEnabled=payload.get("llm_gateway_enabled"),
        llmGatewayUrl=payload.get("llm_gateway_url"),
        llmGatewayKey=payload.get("llm_gateway_key"),
    )

    logger.info(f"guardrail_enabled: {chat.guardrail_enabled}")
    logger.info(f"memory_enabled: {chat.memory_enabled}")
    logger.info(f"llm_gateway_enabled: {chat.llm_gateway_enabled}")

    # Normalize selected MCP names and always attach memory when Memory toggle is on
    mcp_servers = [str(s).strip() for s in (mcp_servers or []) if str(s).strip()]
    if chat.memory_enabled and "memory" not in mcp_servers:
        mcp_servers = mcp_servers + ["memory"]
        logger.info("memory_enabled: appended 'memory' MCP server")
    elif chat.memory_enabled:
        logger.info("memory_enabled: 'memory' MCP already selected")
    elif "memory" in mcp_servers:
        logger.info("memory MCP selected via MCP picker (memory_enabled=False)")

    runtime_session_id = payload.get("runtime_session_id")
    if not runtime_session_id:
        runtime_session_id = chat._runtime_session_id()
    logger.info(f"runtime_session_id: {runtime_session_id}")
    chat.set_checkpoint_session_id(runtime_session_id)

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
            app, config = await chat.create_agent(
                mcp_servers,
                skill_list,
                runtime_session_id=runtime_session_id,
            )
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

        message_content = query or ""
        if files:
            file_summaries = []
            for file_ref in files:
                file_name = chat._file_name_from_ref(file_ref)
                logger.info(f"analyzing uploaded file: {file_ref}")
                try:
                    summary = chat.get_summary_of_uploaded_file(file_ref, prompt=query or "")
                except Exception as e:
                    logger.error(f"Failed to summarize file {file_ref}: {traceback.format_exc()}")
                    summary = f"파일 분석 중 오류가 발생했습니다: {e}"
                file_summaries.append(
                    f"선택한 파일({file_name})의 내용을 요약하면 아래와 같습니다.\n\n{summary}"
                )
            message_content = (message_content + "\n\n" if message_content else "") + "\n\n".join(
                file_summaries
            )
            logger.info(f"message_content with file summaries length: {len(message_content)}")

        inputs = {
            "messages": [HumanMessage(content=message_content)]
        }

        result_text = ""
        tool_used = False
        tool_input_list: dict[str, str] = {}
        tool_name_by_id: dict[str, str] = {}
        tool_args_by_id: dict[str, dict] = {}
        last_tool_args_json: dict[str, str] = {}
        yielded_tool_ids: set[str] = set()
        partial_json_tools: set[str] = set()
        stop_reason: str | None = None

        def remember_stop_reason(message) -> None:
            nonlocal stop_reason
            meta = getattr(message, "response_metadata", None) or {}
            if not isinstance(meta, dict):
                return
            reason = meta.get("stopReason") or meta.get("stop_reason")
            if reason:
                stop_reason = reason
                logger.info(f"[stop_reason] {stop_reason}")

        def remember_tool_args(tool_use_id: str, tool_name: str, args) -> dict | None:
            if not tool_use_id or not tool_name:
                return None
            tool_name_by_id[tool_use_id] = tool_name
            if isinstance(args, dict):
                if not args:
                    return None
                tool_args_by_id[tool_use_id] = args
            args_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
            if last_tool_args_json.get(tool_use_id) == args_json:
                return None
            last_tool_args_json[tool_use_id] = args_json
            yielded_tool_ids.add(tool_use_id)
            logger.info(
                "tool_name: %s, content: %s, toolUseId: %s",
                tool_name,
                args,
                tool_use_id,
            )
            return {
                "tool": tool_name,
                "input": args,
                "toolUseId": tool_use_id,
            }

        def tool_call_fields(tool_call) -> tuple[str, str, dict]:
            if isinstance(tool_call, dict):
                return (
                    tool_call.get("id", ""),
                    tool_call.get("name", ""),
                    tool_call.get("args", {}) if isinstance(tool_call.get("args"), dict) else {},
                )
            return (
                getattr(tool_call, "id", "") or "",
                getattr(tool_call, "name", "") or "",
                getattr(tool_call, "args", {}) or {},
            )

        def args_from_ai_message(message: AIMessage, tool_use_id: str) -> dict:
            for tc in getattr(message, "tool_calls", None) or []:
                tid, _, args = tool_call_fields(tc)
                if tid == tool_use_id and args:
                    return args
            content = message.content
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    if item.get("id") == tool_use_id:
                        input_val = item.get("input")
                        if isinstance(input_val, dict) and input_val:
                            return input_val
            return {}

        async def args_from_graph_state(tool_use_id: str) -> dict:
            try:
                state = await app.aget_state(config)
            except Exception:
                logger.exception("Failed to read graph state for tool args")
                return {}
            messages = (state.values or {}).get("messages", [])
            for message in reversed(messages):
                if isinstance(message, AIMessage):
                    args = args_from_ai_message(message, tool_use_id)
                    if args:
                        return args
            return {}

        def yield_tool_args(tool_use_id: str, tool_name: str, args) -> dict | None:
            payload = remember_tool_args(tool_use_id, tool_name, args)
            return payload

        def resolved_tool_args(tool_use_id: str) -> dict:
            if tool_use_id in tool_args_by_id:
                return tool_args_by_id[tool_use_id]
            args_raw = tool_input_list.get(tool_use_id, "")
            if not args_raw:
                return {}
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                return {"_raw": args_raw}
            if isinstance(parsed, dict):
                tool_args_by_id[tool_use_id] = parsed
                return parsed
            return {"value": parsed}

        # call_model이 chain.astream을 쓰므로 LLM 토큰/청크가 그래프로 전달됨 (langgraph_agent.call_model)
        async for stream in app.astream(inputs, config, stream_mode="messages"):
            chunk = stream[0] if isinstance(stream, (list, tuple)) and stream else stream

            if isinstance(chunk, AIMessageChunk):
                remember_stop_reason(chunk)
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
                            tool_name = item.get("name", "") or tool_name_by_id.get(tool_use_id, "")
                            if tool_use_id and tool_name:
                                tool_name_by_id[tool_use_id] = tool_name
                                tool_input_list.setdefault(tool_use_id, "")
                            # Bedrock sends input={} before partial_json; skip empty placeholders.
                            if "input" in item and item.get("input") is not None:
                                input_val = item.get("input")
                                if isinstance(input_val, dict) and input_val:
                                    payload = yield_tool_args(
                                        tool_use_id,
                                        tool_name_by_id.get(tool_use_id, tool_name),
                                        input_val,
                                    )
                                    if payload:
                                        yield payload
                            if "partial_json" in item:
                                partial_json_tools.add(tool_use_id)
                                pj = item.get("partial_json", "") or ""
                                if tool_use_id:
                                    tool_input_list[tool_use_id] = tool_input_list.get(tool_use_id, "") + pj
                                args_raw = tool_input_list.get(tool_use_id, "")
                                if tool_use_id and args_raw:
                                    try:
                                        args_obj = json.loads(args_raw)
                                        payload = yield_tool_args(
                                            tool_use_id,
                                            tool_name_by_id.get(tool_use_id, tool_name),
                                            args_obj,
                                        )
                                        if payload:
                                            yield payload
                                    except json.JSONDecodeError:
                                        pass
                        elif item.get("type") == "function_call":
                            # OpenAI Responses API
                            tool_use_id = item.get("call_id") or item.get("id", "")
                            tool_name = item.get("name", "") or tool_name_by_id.get(tool_use_id, "")
                            if tool_use_id and tool_name:
                                tool_name_by_id[tool_use_id] = tool_name
                                tool_input_list.setdefault(tool_use_id, "")
                            if "arguments" in item and tool_use_id:
                                arguments = item.get("arguments", "")
                                if not isinstance(arguments, str):
                                    arguments = str(arguments)
                                tool_input_list[tool_use_id] = arguments
                                if arguments:
                                    try:
                                        args_obj = json.loads(arguments)
                                        payload = yield_tool_args(
                                            tool_use_id,
                                            tool_name_by_id.get(tool_use_id, tool_name),
                                            args_obj,
                                        )
                                        if payload:
                                            yield payload
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
                        if not tid or not name or not args:
                            continue
                        payload = yield_tool_args(tid, name, args)
                        if payload:
                            yield payload

            elif isinstance(chunk, AIMessage):
                remember_stop_reason(chunk)
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
                        elif isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_use_id = item.get("id", "")
                            tool_name = item.get("name", "") or tool_name_by_id.get(tool_use_id, "")
                            input_val = item.get("input")
                            if tool_use_id and tool_name and isinstance(input_val, dict) and input_val:
                                payload = yield_tool_args(tool_use_id, tool_name, input_val)
                                if payload:
                                    yield payload
                        elif isinstance(item, dict) and item.get("type") == "function_call":
                            tool_use_id = item.get("call_id") or item.get("id", "")
                            tool_name = item.get("name", "") or tool_name_by_id.get(tool_use_id, "")
                            arguments = item.get("arguments", "")
                            if tool_use_id and tool_name:
                                tool_name_by_id[tool_use_id] = tool_name
                                if isinstance(arguments, str) and arguments:
                                    try:
                                        input_val = json.loads(arguments)
                                    except json.JSONDecodeError:
                                        input_val = None
                                    if isinstance(input_val, dict) and input_val:
                                        payload = yield_tool_args(tool_use_id, tool_name, input_val)
                                        if payload:
                                            yield payload
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
                        if not tid or not name or not args:
                            continue
                        payload = yield_tool_args(tid, name, args)
                        if payload:
                            yield payload

            elif isinstance(chunk, ToolMessage):
                logger.info(f"ToolMessage: {chunk.name}, {chunk.content}")
                tool_used = True
                tool_use_id = chunk.tool_call_id or ""
                tool_name = chunk.name or tool_name_by_id.get(tool_use_id, "")
                if tool_use_id and tool_name:
                    args_obj = resolved_tool_args(tool_use_id)
                    if not args_obj:
                        args_obj = await args_from_graph_state(tool_use_id)
                    payload = yield_tool_args(tool_use_id, tool_name, args_obj)
                    if payload:
                        yield payload
                yield {
                    "toolResult": chunk.content,
                    "toolUseId": tool_use_id,
                    "tool": tool_name,
                }

        for tool_use_id, args_raw in tool_input_list.items():
            if not args_raw or tool_use_id in tool_args_by_id:
                continue
            try:
                args_obj = json.loads(args_raw)
            except json.JSONDecodeError:
                continue
            payload = yield_tool_args(
                tool_use_id,
                tool_name_by_id.get(tool_use_id, ""),
                args_obj,
            )
            if payload:
                yield payload

        skip_memory = False
        if not result_text.strip():
            if stop_reason == "content_filtered":
                result_text = (
                    "요청이 모델 안전 정책에 의해 차단되었습니다. "
                    "다른 모델로 시도하거나 질문을 바꿔 주세요."
                )
                skip_memory = True
            elif stop_reason == "guardrail_intervened":
                result_text = (
                    "요청이 Guardrail 안전 정책에 의해 차단되었습니다. "
                    "질문을 바꿔 주세요."
                )
                skip_memory = True
            elif stop_reason == "refusal":
                result_text = (
                    "모델이 이 요청에 대한 응답을 거부했습니다. "
                    "다른 모델로 시도하거나 질문을 바꿔 주세요."
                )
                skip_memory = True
            else:
                result_text = "답변을 찾지 못하였습니다."

        if chat.memory_enabled and not skip_memory:
            chat.save_to_memory(query, result_text)

        final_output = {
            "messages": [{"role": "assistant", "content": result_text}],
            "image_url": [],
        }
        logger.info(f"final_output: {result_text[:200]!r}...")
        yield {"result": final_output}
    finally:
        await chat.persist_checkpoint_to_session_storage()

if __name__ == "__main__":
    app.run()

