"""Measure Bedrock prompt caching for the LangGraph call_model path.

Simulates a 2-step tool loop with the same cache helpers used in
langgraph_agent.call_model (Claude/Nova cache_control or GPT explicit).

Usage:
  cd runtime_agent/langgraph
  python test_prompt_caching.py
  python test_prompt_caching.py --model-id openai.gpt-5.6-sol --region us-east-2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import bedrock_data_retention
import langgraph_agent as lg
import skill


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLAUDE_MODEL = "us.anthropic.claude-sonnet-5"
DEFAULT_GPT_MODEL = "openai.gpt-5.6-sol"
DEFAULT_SKILLS = [
    "skill-creator",
    "pptx",
    "xlsx",
    "myslide",
    "docx",
    "pdf",
    "frontend-design",
]


@dataclass
class CacheStats:
    label: str
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int

    @property
    def billed_input_like(self) -> int:
        """Approximate total input footprint (uncached + cache write + cache read)."""
        return self.input_tokens + self.cache_creation + self.cache_read

    @property
    def cache_hit_ratio(self) -> float:
        total = self.billed_input_like
        if total <= 0:
            return 0.0
        return self.cache_read / total


def summarize_token_savings(stats_list: list[CacheStats]) -> dict[str, Any]:
    """Compare total input tokens with vs without prompt caching."""
    total_input = sum(s.billed_input_like for s in stats_list)
    total_cache_read = sum(s.cache_read for s in stats_list)
    total_cache_creation = sum(s.cache_creation for s in stats_list)
    total_uncached = sum(s.input_tokens for s in stats_list)
    tokens_without_reuse = total_uncached + total_cache_creation
    reduction_ratio = (total_cache_read / total_input) if total_input else 0.0
    return {
        "calls": len(stats_list),
        "total_input_tokens_without_reuse": total_input,
        "tokens_processed_or_written": tokens_without_reuse,
        "tokens_reused_via_cache_read": total_cache_read,
        "input_token_reduction_pct": round(reduction_ratio * 100, 1),
        "formula": (
            "reduction_% = sum(cache_read) / sum(input + cache_creation + cache_read)"
        ),
    }


def _load_region() -> str:
    config_path = os.path.join(SCRIPT_DIR, "config.json")
    if not os.path.isfile(config_path):
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(SCRIPT_DIR)),
            "application",
            "config.json",
        )
    with open(config_path, encoding="utf-8") as f:
        return json.load(f).get("region", "us-west-2")


def _extract_cache_stats(label: str, message: AIMessage) -> CacheStats:
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") if isinstance(usage, dict) else {}
    details = details or {}
    rm = (getattr(message, "response_metadata", None) or {}).get("usage") or {}

    cache_creation = int(
        details.get("cache_creation")
        or details.get("cache_write_tokens")
        or rm.get("cache_write_input_tokens")
        or rm.get("cacheWriteInputTokens")
        or 0
    )
    cache_read = int(
        details.get("cache_read")
        or details.get("cached_tokens")
        or rm.get("cache_read_input_tokens")
        or rm.get("cacheReadInputTokens")
        or 0
    )
    input_tokens = int(
        usage.get("input_tokens")
        or rm.get("input_tokens")
        or rm.get("prompt_tokens")
        or rm.get("inputTokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or rm.get("output_tokens")
        or rm.get("completion_tokens")
        or rm.get("outputTokens")
        or 0
    )
    return CacheStats(
        label=label,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation=cache_creation,
        cache_read=cache_read,
    )


def _build_system_prompt(skill_list: list[str]) -> str:
    skill_info = skill.get_skill_info(skill_list)
    return skill.build_skill_prompt(skill_info)


def _build_tools() -> list:
    tools = list(lg.get_builtin_tools())
    for t in skill.get_skill_tools():
        if t.name not in {x.name for x in tools}:
            tools.append(t)

    @tool
    def echo_cache_probe(text: str) -> str:
        """Echo text. Used only by the prompt-caching probe."""
        return text

    if "echo_cache_probe" not in {t.name for t in tools}:
        tools.append(echo_cache_probe)
    return tools


def _build_chatbedrock(model_id: str, region: str) -> ChatBedrock:
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(retries={"max_attempts": 8}, read_timeout=180),
    )
    return ChatBedrock(
        model_id=model_id,
        client=client,
        model_kwargs={"max_tokens": 256},
        region_name=region,
        provider="anthropic",
    )


def _build_mantle_chat(model_id: str, region: str) -> ChatOpenAI:
    def bearer_token_provider() -> str:
        return bedrock_data_retention.get_bedrock_bearer_token(region)

    return ChatOpenAI(
        model=model_id,
        api_key=bearer_token_provider,
        base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
        use_responses_api=True,
        max_tokens=256,
    )


def _print_stats(stats: CacheStats) -> None:
    print(
        f"  [{stats.label}] "
        f"input={stats.input_tokens} "
        f"cache_creation={stats.cache_creation} "
        f"cache_read={stats.cache_read} "
        f"output={stats.output_tokens} "
        f"hit_ratio={stats.cache_hit_ratio:.1%}"
    )


def _prepare_cached_model(
    *,
    model_id: str,
    region: str,
    tools: list,
    run_nonce: str,
):
    use_gpt = lg._supports_gpt_explicit_caching("openai", model_id)
    if use_gpt:
        chat_model = _build_mantle_chat(model_id, region)
        model = chat_model.bind_tools(tools).bind(
            prompt_cache_key=f"probe:{run_nonce}",
            prompt_cache_options=lg.GPT_PROMPT_CACHE_OPTIONS,
        )
        cache_mode = "gpt-explicit"
    else:
        chat_model = _build_chatbedrock(model_id, region)
        model = chat_model.bind_tools(tools).bind(cache_control=lg.PROMPT_CACHE_CONTROL)
        cache_mode = "bedrock-converse"
    return model, cache_mode


def _prepare_system_message(system_for_run: str, model_id: str) -> Any:
    if lg._supports_gpt_explicit_caching("openai", model_id):
        return lg._system_message_with_gpt_cache(system_for_run)
    return lg._system_message_with_bedrock_cache(system_for_run)


def run_tool_loop_probe(
    *,
    model_id: str = DEFAULT_CLAUDE_MODEL,
    region: str | None = None,
    skill_list: list[str] | None = None,
) -> dict[str, Any]:
    region = region or _load_region()
    skill_list = skill_list or DEFAULT_SKILLS
    system = _build_system_prompt(skill_list)
    tools = _build_tools()

    print("=== Prompt Caching Probe ===")
    print(f"region={region}")
    print(f"model_id={model_id}")
    print(f"skills={skill_list}")
    print(f"system_chars={len(system)} (~{len(system) // 4} tokens)")
    print(f"tools={len(tools)} names={[t.name for t in tools]}")

    run_nonce = uuid.uuid4().hex[:8]
    system_for_run = (
        f"{system}\n\n## Cache probe run id\n"
        f"- run_id: {run_nonce}\n"
    )
    model, cache_mode = _prepare_cached_model(
        model_id=model_id,
        region=region,
        tools=tools,
        run_nonce=run_nonce,
    )
    system_msg = _prepare_system_message(system_for_run, model_id)
    print(f"run_id={run_nonce} cache_mode={cache_mode} (forces cold cache on call1)")

    user = HumanMessage(
        content=(
            "echo_cache_probe 도구만 사용해서 text='hello-cache'를 호출하세요. "
            "다른 도구는 쓰지 마세요."
        )
    )

    print("\n--- Call 1 (expect cache_creation) ---")
    r1 = model.invoke([system_msg, user])
    s1 = _extract_cache_stats("call1", r1)
    _print_stats(s1)
    print(f"  tool_calls={getattr(r1, 'tool_calls', None)}")

    if getattr(r1, "tool_calls", None):
        tc = r1.tool_calls[0]
        tool_msg = ToolMessage(content="hello-cache", tool_call_id=tc["id"])
        history = [system_msg, user, r1, tool_msg]
    else:
        synthetic = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "echo_cache_probe",
                    "args": {"text": "hello-cache"},
                    "id": "toolu_probe_1",
                    "type": "tool_call",
                }
            ],
        )
        tool_msg = ToolMessage(content="hello-cache", tool_call_id="toolu_probe_1")
        history = [system_msg, user, synthetic, tool_msg]
        print("  note: model did not emit tool_calls; using synthetic tool loop")

    print("\n--- Call 2 (expect cache_read of call1 prefix) ---")
    r2 = model.invoke(history)
    s2 = _extract_cache_stats("call2", r2)
    _print_stats(s2)
    print(f"  content={str(r2.content)[:160]!r}")

    savings = summarize_token_savings([s1, s2])
    summary = {
        "model_id": model_id,
        "region": region,
        "cache_mode": cache_mode,
        "skills": skill_list,
        "system_chars": len(system),
        "tool_count": len(tools),
        "call1": {
            "input_tokens": s1.input_tokens,
            "cache_creation": s1.cache_creation,
            "cache_read": s1.cache_read,
            "output_tokens": s1.output_tokens,
            "input_footprint": s1.billed_input_like,
            "hit_ratio": round(s1.cache_hit_ratio, 4),
        },
        "call2": {
            "input_tokens": s2.input_tokens,
            "cache_creation": s2.cache_creation,
            "cache_read": s2.cache_read,
            "output_tokens": s2.output_tokens,
            "input_footprint": s2.billed_input_like,
            "hit_ratio": round(s2.cache_hit_ratio, 4),
        },
        "savings": savings,
    }

    print("\n=== Token savings (2-call tool loop) ===")
    print(
        f"  without caching (full input each call): "
        f"{savings['total_input_tokens_without_reuse']} tokens"
    )
    print(
        f"  reused via cache_read: "
        f"{savings['tokens_reused_via_cache_read']} tokens"
    )
    print(
        f"  still processed/written (uncached + cache_creation): "
        f"{savings['tokens_processed_or_written']} tokens"
    )
    print(
        f"  input token reduction: "
        f"{savings['input_token_reduction_pct']}%  "
        f"({savings['formula']})"
    )

    print("\n=== Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if s1.cache_creation <= 0 and s1.cache_read <= 0:
        print("\nWARN: call1 had neither cache_creation nor cache_read")
    elif s1.cache_creation <= 0 and s1.cache_read > 0:
        print(
            "\nNOTE: call1 was a cache hit from an identical prior prefix "
            "(unexpected with unique run_id)"
        )
    if s2.cache_read <= 0:
        print("\nWARN: call2 cache_read=0 (cache miss — check TTL / model / markers)")
    else:
        print(
            f"\nOK: tool-loop reused {s2.cache_read} tokens on call2 "
            f"({s2.cache_hit_ratio:.1%} of call2); "
            f"overall input tokens reduced by "
            f"{savings['input_token_reduction_pct']}% across both calls"
        )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe LangGraph prompt caching")
    parser.add_argument(
        "--model-id",
        default=DEFAULT_CLAUDE_MODEL,
        help=f"Bedrock model id (default: {DEFAULT_CLAUDE_MODEL})",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region (default: config.json region)",
    )
    args = parser.parse_args()

    summary = run_tool_loop_probe(model_id=args.model_id, region=args.region)
    ok = (
        summary["call1"]["cache_creation"] > 0
        and summary["call2"]["cache_read"] > 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
