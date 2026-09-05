"""AgentCore runtime client facade (LangGraph AgentCore invoke + SSE)."""

from __future__ import annotations

import json
import logging
import sys

import boto3
from botocore.config import Config

try:
    from application.agentcore_runtime import (
        bedrock_region,
        load_agentcore_config,
        projectName,
        runtime_session_id_for,
    )
    from application.agentcore_sse import (
        _finalize_agent_result,
        _process_sse_event,
        add_notification,
        normalize_bedrock_message_content,
        tool_info_list,
        tool_name_list,
        tool_result_list,
        tool_slot_update,
        update_streaming_result,
    )
    from application.tool_result_parsers import get_tool_info
except ImportError:
    from agentcore_runtime import (
        bedrock_region,
        load_agentcore_config,
        projectName,
        runtime_session_id_for,
    )
    from agentcore_sse import (
        _finalize_agent_result,
        _process_sse_event,
        add_notification,
        normalize_bedrock_message_content,
        tool_info_list,
        tool_name_list,
        tool_result_list,
        tool_slot_update,
        update_streaming_result,
    )
    from tool_result_parsers import get_tool_info

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("agentcore_client")

__all__ = [
    "AgentCoreService",
    "run_agent",
    "load_agentcore_config",
    "runtime_session_id_for",
    "get_tool_info",
    "add_notification",
    "update_streaming_result",
    "tool_slot_update",
    "normalize_bedrock_message_content",
]

AGENTCORE_READ_TIMEOUT_SECONDS = 1200  # SSE gaps while agent runs tools / long LLM turns
AGENTCORE_CONNECT_TIMEOUT_SECONDS = 60
AGENTCORE_MAX_ATTEMPTS = 3
DEFAULT_AGENT_TYPE = "langgraph"


class AgentCoreService:
    """Orchestrates AgentCore runtime resolution, invoke, and stream processing."""

    def __init__(self, region: str | None = None, agent_type: str = DEFAULT_AGENT_TYPE):
        self.region = region or bedrock_region
        self.agent_type = agent_type

    def create_runtime_client(self):
        boto_config = Config(
            read_timeout=AGENTCORE_READ_TIMEOUT_SECONDS,
            connect_timeout=AGENTCORE_CONNECT_TIMEOUT_SECONDS,
            retries={"mode": "standard", "max_attempts": AGENTCORE_MAX_ATTEMPTS},
        )
        return boto3.client(
            "bedrock-agentcore",
            region_name=self.region,
            config=boto_config,
        )

    def resolve_runtime_arn(self, agent_type: str | None = None) -> str | None:
        at = agent_type or self.agent_type
        runtime_name = projectName.replace("-", "_") + "_" + at
        return load_agentcore_config(runtime_name, agent_type=at)

    def build_payload(
        self,
        *,
        prompt,
        user_id,
        mcp_servers,
        model_name,
        runtime_session_id,
        skill_list=None,
        guardrail_enabled=None,
        memory_enabled=None,
        llm_gateway_enabled=None,
        llm_gateway_url=None,
        llm_gateway_key=None,
        files=None,
    ) -> str:
        return json.dumps(
            {
                "prompt": prompt,
                "mcp_servers": mcp_servers,
                "model_name": model_name,
                "user_id": user_id,
                "skill_list": skill_list or [],
                "guardrail_enabled": (
                    bool(guardrail_enabled) if guardrail_enabled is not None else True
                ),
                "memory_enabled": (
                    bool(memory_enabled) if memory_enabled is not None else True
                ),
                "llm_gateway_enabled": (
                    bool(llm_gateway_enabled) if llm_gateway_enabled is not None else False
                ),
                "llm_gateway_url": llm_gateway_url or "",
                "llm_gateway_key": llm_gateway_key or "",
                "runtime_session_id": runtime_session_id,
                "files": files or [],
            }
        )

    def process_event_stream(self, response, notification_queue, stream_state: dict) -> None:
        if "text/event-stream" not in response.get("contentType", ""):
            return

        for line in response["response"].iter_lines(chunk_size=10):
            line = line.decode("utf-8")
            if line:
                print(f"-> {line}")

            if not line.startswith("data: "):
                continue

            data = line[6:].strip()
            # Do not dedupe by payload string: stream text deltas like
            # {"data": " "} / {"data": "니다."} legitimately repeat in one turn.
            if not data:
                continue

            try:
                data_json = json.loads(data)
                _process_sse_event(
                    data_json,
                    notification_queue,
                    stream_state,
                    agent_type=self.agent_type,
                )
            except json.JSONDecodeError:
                logger.info("Not JSON: %s", data)
            except Exception as e:
                logger.error("Error processing data: %s", e, exc_info=True)
                continue

    def run(
        self,
        prompt,
        user_id,
        mcp_servers,
        model_name,
        runtime_session_id,
        notification_queue=None,
        skill_list=None,
        guardrail_enabled=None,
        memory_enabled=None,
        llm_gateway_enabled=None,
        llm_gateway_url=None,
        llm_gateway_key=None,
        files=None,
    ):
        tool_info_list.clear()
        tool_result_list.clear()
        tool_name_list.clear()
        if notification_queue is not None:
            notification_queue.reset()

        references: list = []
        image_url: list = []

        logger.info("user_id: %s", user_id)

        payload = self.build_payload(
            prompt=prompt,
            user_id=user_id,
            mcp_servers=mcp_servers,
            model_name=model_name,
            runtime_session_id=runtime_session_id,
            skill_list=skill_list,
            guardrail_enabled=guardrail_enabled,
            memory_enabled=memory_enabled,
            llm_gateway_enabled=llm_gateway_enabled,
            llm_gateway_url=llm_gateway_url,
            llm_gateway_key=llm_gateway_key,
            files=files,
        )

        agent_runtime_arn = self.resolve_runtime_arn()
        print(f"agent_runtime_arn: {agent_runtime_arn}")
        logger.info("agent_runtime_arn: %s", agent_runtime_arn)
        logger.info("Payload: %s", payload)

        if agent_runtime_arn is None:
            logger.error("agent_runtime_arn is not found")
            return "Error: agent_runtime_arn is not found", []

        try:
            agent_core_client = self.create_runtime_client()
            logger.info("runtime_session_id: %s", runtime_session_id)
            response = agent_core_client.invoke_agent_runtime(
                agentRuntimeArn=agent_runtime_arn,
                runtimeSessionId=runtime_session_id,
                payload=payload,
                qualifier="DEFAULT",
            )

            stream_state = {
                "result": "",
                "current": "",
                "image_url": image_url,
                "references": references,
                "tool_input_cache": {},
            }
            self.process_event_stream(response, notification_queue, stream_state)

            result = _finalize_agent_result(
                stream_state["result"],
                stream_state["current"],
                stream_state["references"],
                notification_queue,
            )
            image_url = stream_state["image_url"]

            logger.info("result: %s", result)
            return result, image_url

        except Exception as exc:
            from botocore.exceptions import ReadTimeoutError

            if isinstance(exc, ReadTimeoutError):
                logger.exception(
                    "AgentCore SSE read timed out after %ss (idle gap between stream chunks)",
                    AGENTCORE_READ_TIMEOUT_SECONDS,
                )
                return (
                    "AgentCore 응답 스트림이 시간 초과되었습니다. "
                    "작업이 길면 잠시 후 다시 시도하거나, 질문을 나눠 보내 주세요.",
                    [],
                )
            logger.exception("Unexpected error while running agent")
            return "An error occurred processing your request", []


def run_agent(
    prompt,
    user_id,
    mcp_servers,
    model_name,
    runtime_session_id,
    notification_queue=None,
    skill_list=None,
    guardrail_enabled=None,
    memory_enabled=None,
    llm_gateway_enabled=None,
    llm_gateway_url=None,
    llm_gateway_key=None,
    files=None,
):
    return AgentCoreService().run(
        prompt,
        user_id,
        mcp_servers,
        model_name,
        runtime_session_id,
        notification_queue=notification_queue,
        skill_list=skill_list,
        guardrail_enabled=guardrail_enabled,
        memory_enabled=memory_enabled,
        llm_gateway_enabled=llm_gateway_enabled,
        llm_gateway_url=llm_gateway_url,
        llm_gateway_key=llm_gateway_key,
        files=files,
    )
