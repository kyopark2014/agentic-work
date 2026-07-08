"""CloudWatch custom metrics and dashboard helpers for LangGraph AgentCore runtime."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

METRIC_NAMESPACE = "LangGraph/AgentCoreRuntime"
AGENTCORE_NAMESPACE = "AWS/Bedrock-AgentCore"
AGENTCORE_SERVICE = "AgentCore.Runtime"
BEDROCK_NAMESPACE = "AWS/Bedrock"
BEDROCK_USAGE_DASHBOARD_NAME = "Bedrock-Usage-Dashboard"
INVOKE_OPERATION = "InvokeAgentRuntime"

# AgentCore Runtime pricing (USD)
RUNTIME_CPU_COST_PER_VCPU_HOUR = 0.0895
RUNTIME_MEMORY_COST_PER_GB_HOUR = 0.00945
COST_DISPLAY_DECIMALS = 3

# Bedrock on-demand pricing per 1M tokens (USD). Used for estimated model cost.
MODEL_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "us.anthropic.claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "us.anthropic.claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-fable-5": {"input": 3.0, "output": 15.0},
    "us.amazon.nova-premier-v1:0": {"input": 2.5, "output": 12.5},
    "us.amazon.nova-pro-v1:0": {"input": 0.80, "output": 3.20},
    "us.amazon.nova-lite-v1:0": {"input": 0.06, "output": 0.24},
    "us.amazon.nova-micro-v1:0": {"input": 0.035, "output": 0.14},
    "us.amazon.nova-2-lite-v1:0": {"input": 0.06, "output": 0.24},
    "openai.gpt-5.4": {"input": 1.25, "output": 10.0},
    "openai.gpt-5.5": {"input": 1.25, "output": 10.0},
    "openai.gpt-oss-120b-1:0": {"input": 0.30, "output": 0.60},
    "openai.gpt-oss-20b-1:0": {"input": 0.10, "output": 0.30},
}

DEFAULT_MODEL_PRICING = {"input": 3.0, "output": 15.0}

_cloudwatch_client = None


def _get_cloudwatch_client():
    global _cloudwatch_client
    if _cloudwatch_client is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        _cloudwatch_client = boto3.client("cloudwatch", region_name=region)
    return _cloudwatch_client


def _load_runtime_context() -> dict[str, str]:
    project_name = "langgraph-runtime"
    runtime_name = "runtime_langgraph"
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"

    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_name = config.get("projectName", project_name)
        region = config.get("region", region)
        arn = config.get("agent_runtime_arn", "")
        if arn:
            runtime_name = arn.rsplit("/", 1)[-1]
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    return {
        "ProjectName": project_name,
        "AgentRuntimeName": runtime_name,
        "Region": region,
    }


def _resolve_model_pricing(model_id: str) -> dict[str, float]:
    if model_id in MODEL_PRICING_PER_MILLION:
        return MODEL_PRICING_PER_MILLION[model_id]

    for key, pricing in MODEL_PRICING_PER_MILLION.items():
        if model_id.startswith(key) or key in model_id:
            return pricing

    return DEFAULT_MODEL_PRICING


def extract_token_usage(message: Any) -> dict[str, int]:
    """Extract token counts from a LangChain AIMessage or chunk."""
    usage: dict[str, int] = {}

    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        usage["input_tokens"] = int(usage_metadata.get("input_tokens") or 0)
        usage["output_tokens"] = int(usage_metadata.get("output_tokens") or 0)
        usage["total_tokens"] = int(
            usage_metadata.get("total_tokens")
            or usage["input_tokens"] + usage["output_tokens"]
        )

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        bedrock_usage = response_metadata.get("usage") or response_metadata.get("token_usage") or {}
        if isinstance(bedrock_usage, dict):
            usage.setdefault(
                "input_tokens",
                int(bedrock_usage.get("input_tokens") or bedrock_usage.get("prompt_tokens") or 0),
            )
            usage.setdefault(
                "output_tokens",
                int(bedrock_usage.get("output_tokens") or bedrock_usage.get("completion_tokens") or 0),
            )
            usage.setdefault(
                "total_tokens",
                int(
                    bedrock_usage.get("total_tokens")
                    or usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                ),
            )

    return {k: v for k, v in usage.items() if v > 0}


def estimate_model_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pricing = _resolve_model_pricing(model_id)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 8)


def publish_token_metrics(model_id: str, message: Any) -> None:
    """Publish token usage and estimated model cost to CloudWatch."""
    usage = extract_token_usage(message)
    if not usage:
        return

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
    if total_tokens <= 0:
        return

    context = _load_runtime_context()
    model_short = model_id.rsplit(".", 1)[-1][:64] if model_id else "unknown"
    dimensions = [
        {"Name": "ProjectName", "Value": context["ProjectName"]},
        {"Name": "AgentRuntimeName", "Value": context["AgentRuntimeName"]},
        {"Name": "ModelId", "Value": model_short},
    ]

    estimated_cost = estimate_model_cost_usd(model_id, input_tokens, output_tokens)
    timestamp = None
    try:
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc)
    except Exception:
        pass

    metric_data = [
        {"MetricName": "InputTokens", "Value": float(input_tokens), "Unit": "Count"},
        {"MetricName": "OutputTokens", "Value": float(output_tokens), "Unit": "Count"},
        {"MetricName": "TotalTokens", "Value": float(total_tokens), "Unit": "Count"},
        {"MetricName": "EstimatedModelCostUSD", "Value": estimated_cost, "Unit": "None"},
        {"MetricName": "LLMInvocations", "Value": 1.0, "Unit": "Count"},
    ]

    for entry in metric_data:
        entry["Dimensions"] = dimensions
        if timestamp:
            entry["Timestamp"] = timestamp

    try:
        _get_cloudwatch_client().put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
        logger.info(
            "Published token metrics: model=%s input=%s output=%s cost=$%.6f",
            model_short,
            input_tokens,
            output_tokens,
            estimated_cost,
        )
    except Exception as exc:
        logger.warning("Failed to publish CloudWatch token metrics: %s", exc)


def dashboard_name(project_name: str) -> str:
    safe_name = project_name.replace(" ", "-")
    return f"{safe_name}-monitoring"


def _runtime_base_name(agent_runtime_arn: str) -> str:
    runtime_id = agent_runtime_arn.rsplit("/", 1)[-1]
    if "-" in runtime_id:
        return runtime_id.rsplit("-", 1)[0]
    return runtime_id


def _runtime_name_dimension(agent_runtime_arn: str) -> str:
    return f"{_runtime_base_name(agent_runtime_arn)}::DEFAULT"


def _metric_options(**options: Any) -> dict[str, Any]:
    return options


def _agentcore_invoke_metric(
    metric_name: str,
    agent_runtime_arn: str,
    **options: Any,
) -> list[Any]:
    """AgentCore InvokeAgentRuntime metric (Resource, Operation, Name)."""
    row: list[Any] = [
        AGENTCORE_NAMESPACE,
        metric_name,
        "Resource",
        agent_runtime_arn,
        "Operation",
        INVOKE_OPERATION,
        "Name",
        _runtime_name_dimension(agent_runtime_arn),
    ]
    if options:
        row.append(_metric_options(**options))
    return row


def _agentcore_resource_metric(
    metric_name: str,
    agent_runtime_arn: str,
    **options: Any,
) -> list[Any]:
    """AgentCore runtime resource metric (Resource, Service, Name)."""
    row: list[Any] = [
        AGENTCORE_NAMESPACE,
        metric_name,
        "Resource",
        agent_runtime_arn,
        "Service",
        AGENTCORE_SERVICE,
        "Name",
        _runtime_name_dimension(agent_runtime_arn),
    ]
    if options:
        row.append(_metric_options(**options))
    return row


def _custom_metric_search_expression(
    metric_name: str,
    project_name: str,
    period: int,
    stat: str = "Sum",
) -> str:
    """SEARCH expression for custom metrics published with multiple dimensions."""
    return (
        f"SEARCH('{{{METRIC_NAMESPACE},ProjectName,AgentRuntimeName,ModelId}} "
        f'ProjectName="{project_name}" MetricName="{metric_name}"\', '
        f"'{stat}', {period})"
    )


def _custom_project_metric(
    metric_name: str,
    project_name: str,
    period: int = 300,
    stat: str = "Sum",
    aggregate: bool = True,
    **options: Any,
) -> list[Any]:
    """Custom LangGraph metric query.

    Metrics are published with ProjectName, AgentRuntimeName, and ModelId.
    CloudWatch requires SEARCH (or full dimension match), not ProjectName alone.
    """
    search_expr = _custom_metric_search_expression(metric_name, project_name, period, stat)
    expression = f"SUM({search_expr})" if aggregate else search_expr
    row: dict[str, Any] = {"expression": expression}
    if aggregate and not options.get("id"):
        row["label"] = metric_name
    if options:
        row.update(_metric_options(**options))
    return [row]


def _estimated_cost_source_metrics(
    agent_runtime_arn: str,
    project_name: str,
    period: int = 300,
) -> list[Any]:
    """Hidden metrics used by estimated cost expressions."""
    return [
        _agentcore_resource_metric(
            "CPUUsed-vCPUHours", agent_runtime_arn, id="m1", visible=False
        ),
        _agentcore_resource_metric(
            "MemoryUsed-GBHours", agent_runtime_arn, id="m2", visible=False
        ),
        _custom_project_metric(
            "EstimatedModelCostUSD",
            project_name,
            period=period,
            id="m3",
            visible=False,
        ),
    ]


def _round_cost_expression(expression: str) -> str:
    """Round to fixed decimals using FLOOR (CloudWatch has no ROUND function)."""
    multiplier = 10**COST_DISPLAY_DECIMALS
    return f"FLOOR(({expression}) * {multiplier} + 0.5) / {multiplier}"


def _summary_cost_widget_options() -> dict[str, Any]:
    """Avoid scientific notation (e.g. 9E-3) for small USD amounts in singleValue widgets."""
    return {
        "singleValueFullPrecision": True,
        "setPeriodToTimeRange": True,
    }


def _runtime_cpu_cost_summary_metrics(agent_runtime_arn: str) -> list[Any]:
    return [
        [
            {
                "expression": _round_cost_expression(
                    f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}"
                ),
                "label": "CPU",
                "id": "e1",
            }
        ],
        _agentcore_resource_metric(
            "CPUUsed-vCPUHours", agent_runtime_arn, id="m1", visible=False
        ),
    ]


def _runtime_memory_cost_summary_metrics(agent_runtime_arn: str) -> list[Any]:
    return [
        [
            {
                "expression": _round_cost_expression(
                    f"m1 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}"
                ),
                "label": "Memory",
                "id": "e1",
            }
        ],
        _agentcore_resource_metric(
            "MemoryUsed-GBHours", agent_runtime_arn, id="m1", visible=False
        ),
    ]


def _estimated_cost_component_metrics() -> list[list[dict[str, Any]]]:
    """Stacked cost components; avoids adding missing TimeSeries in one expression."""
    return [
        [
            {
                "expression": f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}",
                "label": "Runtime CPU",
                "id": "e1",
            }
        ],
        [
            {
                "expression": f"m2 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}",
                "label": "Runtime Memory",
                "id": "e2",
            }
        ],
        [{"expression": "m3", "label": "Model", "id": "e3"}],
    ]


def _estimated_total_cost_expression() -> str:
    """Single-value total cost; IF() handles metrics with no data yet."""
    return (
        f"IF(m1, m1, 0) * {RUNTIME_CPU_COST_PER_VCPU_HOUR} + "
        f"IF(m2, m2, 0) * {RUNTIME_MEMORY_COST_PER_GB_HOUR} + "
        f"IF(m3, m3, 0)"
    )


def _bedrock_search_metric(
    metric_name: str,
    stat: str,
    period: int,
    metric_id: str = "e1",
) -> list[list[dict[str, Any]]]:
    """Build a CloudWatch SEARCH metric query for AWS/Bedrock ModelId metrics."""
    return [
        [
            {
                "expression": (
                    f"SEARCH('{{{BEDROCK_NAMESPACE},ModelId}} "
                    f'MetricName=\"{metric_name}\"\', '
                    f"'{stat}', {period})"
                ),
                "id": metric_id,
            }
        ]
    ]


def build_bedrock_usage_dashboard_body(region: str) -> str:
    """Build Bedrock-Usage-Dashboard JSON body with dynamic model discovery."""
    widgets: list[dict[str, Any]] = [
        {
            "type": "text",
            "x": 0,
            "y": 0,
            "width": 24,
            "height": 2,
            "properties": {
                "markdown": (
                    f"# {BEDROCK_USAGE_DASHBOARD_NAME}\n"
                    f"**Region:** `{region}` | **Namespace:** `{BEDROCK_NAMESPACE}`\n\n"
                    "모델 ID는 SEARCH 표현식으로 자동 탐색합니다."
                ),
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 입력 토큰 (24h)",
                "view": "pie",
                "region": region,
                "period": 86400,
                "setPeriodToTimeRange": True,
                "metrics": _bedrock_search_metric("InputTokenCount", "Sum", 86400),
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 출력 토큰 (24h)",
                "view": "pie",
                "region": region,
                "period": 86400,
                "setPeriodToTimeRange": True,
                "metrics": _bedrock_search_metric("OutputTokenCount", "Sum", 86400),
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 8,
            "width": 24,
            "height": 6,
            "properties": {
                "title": "모델별 API 호출 횟수",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 3600,
                "metrics": _bedrock_search_metric("Invocations", "Sum", 3600),
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 14,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 지연 시간 (Average ms)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 3600,
                "metrics": _bedrock_search_metric("InvocationLatency", "Average", 3600),
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 14,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 오류 (Client + Server)",
                "view": "timeSeries",
                "stacked": True,
                "region": region,
                "period": 3600,
                "metrics": [
                    *_bedrock_search_metric("InvocationClientErrors", "Sum", 3600, "e1"),
                    *_bedrock_search_metric("InvocationServerErrors", "Sum", 3600, "e2"),
                ],
            },
        },
    ]
    return json.dumps({"widgets": widgets})


def build_dashboard_body(
    project_name: str,
    agent_runtime_arn: str,
    region: str,
) -> str:
    """Build CloudWatch dashboard JSON body."""
    runtime_id = agent_runtime_arn.rsplit("/", 1)[-1] if agent_runtime_arn else "*"
    dash_name = dashboard_name(project_name)

    def invoke(metric_name: str, **options: Any) -> list[Any]:
        return _agentcore_invoke_metric(metric_name, agent_runtime_arn, **options)

    def resource(metric_name: str, **options: Any) -> list[Any]:
        return _agentcore_resource_metric(metric_name, agent_runtime_arn, **options)

    def custom(metric_name: str, period: int = 300, **options: Any) -> list[Any]:
        return _custom_project_metric(metric_name, project_name, period=period, **options)

    widgets: list[dict[str, Any]] = [
        {
            "type": "text",
            "x": 0,
            "y": 0,
            "width": 24,
            "height": 2,
            "properties": {
                "markdown": (
                    f"# {dash_name}\n"
                    f"**Region:** `{region}` | **Runtime:** `{runtime_id}` | "
                    f"**ARN:** `{agent_runtime_arn or 'N/A'}`\n\n"
                    "토큰 사용량·모델 비용(추정)은 LangGraph 런타임이 발행하는 커스텀 메트릭이며, "
                    "런타임 CPU/메모리 비용은 AgentCore vended 메트릭 기반 추정치입니다. "
                    "실제 청구액은 AWS 청구서를 기준으로 하세요."
                ),
            },
        },
        # Summary: 24h single values
        {
            "type": "metric",
            "x": 0,
            "y": 2,
            "width": 4,
            "height": 4,
            "properties": {
                "title": "Total Tokens (24h)",
                "view": "singleValue",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "metrics": [custom("TotalTokens", period=86400)],
            },
        },
        {
            "type": "metric",
            "x": 4,
            "y": 2,
            "width": 4,
            "height": 4,
            "properties": {
                "title": "Model Cost (24h est.)",
                "view": "singleValue",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                **_summary_cost_widget_options(),
                "metrics": [
                    [{"expression": _round_cost_expression("m1"), "id": "e1"}],
                    _custom_project_metric(
                        "EstimatedModelCostUSD",
                        project_name,
                        period=86400,
                        id="m1",
                        visible=False,
                    ),
                ],
            },
        },
        {
            "type": "metric",
            "x": 8,
            "y": 2,
            "width": 4,
            "height": 4,
            "properties": {
                "title": "Runtime CPU Cost (24h est.)",
                "view": "singleValue",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                **_summary_cost_widget_options(),
                "metrics": _runtime_cpu_cost_summary_metrics(agent_runtime_arn),
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 2,
            "width": 4,
            "height": 4,
            "properties": {
                "title": "Runtime Memory Cost (24h est.)",
                "view": "singleValue",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                **_summary_cost_widget_options(),
                "metrics": _runtime_memory_cost_summary_metrics(agent_runtime_arn),
            },
        },
        {
            "type": "metric",
            "x": 16,
            "y": 2,
            "width": 4,
            "height": 4,
            "properties": {
                "title": "Invocations (24h)",
                "view": "singleValue",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "metrics": [invoke("Invocations")],
            },
        },
        {
            "type": "metric",
            "x": 20,
            "y": 2,
            "width": 4,
            "height": 4,
            "properties": {
                "title": "Total Cost (24h est.)",
                "view": "singleValue",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                **_summary_cost_widget_options(),
                "metrics": [
                    [
                        {
                            "expression": _round_cost_expression(
                                _estimated_total_cost_expression()
                            ),
                            "label": "Total",
                            "id": "e1",
                        }
                    ],
                    *_estimated_cost_source_metrics(
                        agent_runtime_arn, project_name, period=86400
                    ),
                ],
            },
        },
        # Row 1: Invocations & Sessions
        {
            "type": "metric",
            "x": 0,
            "y": 6,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Runtime Invocations",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [invoke("Invocations")],
            },
        },
        {
            "type": "metric",
            "x": 8,
            "y": 6,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Active Sessions",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Average",
                "metrics": [
                    [
                        AGENTCORE_NAMESPACE,
                        "ActiveSessionCount",
                        "Service",
                        AGENTCORE_SERVICE,
                    ]
                ],
            },
        },
        {
            "type": "metric",
            "x": 16,
            "y": 6,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Runtime Latency (p99 ms)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "p99",
                "metrics": [invoke("Latency")],
            },
        },
        # Row 2: Errors
        {
            "type": "metric",
            "x": 0,
            "y": 12,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Errors (System + User)",
                "view": "timeSeries",
                "stacked": True,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [
                    invoke("SystemErrors", label="System Errors"),
                    invoke("UserErrors", label="User Errors"),
                ],
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 12,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Throttles",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [invoke("Throttles")],
            },
        },
        # Row 3: Token usage
        {
            "type": "metric",
            "x": 0,
            "y": 18,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Input Tokens (Sum)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [custom("InputTokens")],
            },
        },
        {
            "type": "metric",
            "x": 8,
            "y": 18,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Output Tokens (Sum)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [custom("OutputTokens")],
            },
        },
        {
            "type": "metric",
            "x": 16,
            "y": 18,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Total Tokens (Sum)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [custom("TotalTokens")],
            },
        },
        # Row 4: Token by model
        {
            "type": "metric",
            "x": 0,
            "y": 24,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Total Tokens by Model",
                "view": "timeSeries",
                "stacked": True,
                "region": region,
                "period": 300,
                "metrics": [
                    [
                        {
                            "expression": _custom_metric_search_expression(
                                "TotalTokens", project_name, 300
                            ),
                            "id": "e1",
                        }
                    ]
                ],
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 24,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "LLM Invocations",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [custom("LLMInvocations")],
            },
        },
        # Row 5: Resource usage
        {
            "type": "metric",
            "x": 0,
            "y": 30,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Runtime CPU (vCPU-Hours)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [resource("CPUUsed-vCPUHours")],
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 30,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Runtime Memory (GB-Hours)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "metrics": [resource("MemoryUsed-GBHours")],
            },
        },
        # Row 6: Cost estimation
        {
            "type": "metric",
            "x": 0,
            "y": 36,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Estimated Model Cost (USD)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                "metrics": [custom("EstimatedModelCostUSD")],
            },
        },
        {
            "type": "metric",
            "x": 8,
            "y": 36,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Estimated Runtime CPU Cost (USD)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                "metrics": [
                    [
                        {
                            "expression": f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}",
                            "label": "CPU Cost (est.)",
                            "id": "e1",
                        }
                    ],
                    resource("CPUUsed-vCPUHours", id="m1", visible=False),
                ],
            },
        },
        {
            "type": "metric",
            "x": 16,
            "y": 36,
            "width": 8,
            "height": 6,
            "properties": {
                "title": "Estimated Runtime Memory Cost (USD)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                "metrics": [
                    [
                        {
                            "expression": f"m1 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}",
                            "label": "Memory Cost (est.)",
                            "id": "e1",
                        }
                    ],
                    resource("MemoryUsed-GBHours", id="m1", visible=False),
                ],
            },
        },
        # Row 7: Total estimated cost
        {
            "type": "metric",
            "x": 0,
            "y": 42,
            "width": 24,
            "height": 6,
            "properties": {
                "title": "Total Estimated Cost (USD) — Model + Runtime CPU + Runtime Memory",
                "view": "timeSeries",
                "stacked": True,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "yAxis": {"left": {"label": "USD", "showUnits": False}},
                "metrics": [
                    *_estimated_cost_component_metrics(),
                    *_estimated_cost_source_metrics(agent_runtime_arn, project_name),
                ],
            },
        },
    ]

    return json.dumps({"widgets": widgets})


def create_bedrock_usage_dashboard(region: str) -> str | None:
    """Create or update the Bedrock usage dashboard. Returns dashboard name."""
    name = BEDROCK_USAGE_DASHBOARD_NAME
    body = build_bedrock_usage_dashboard_body(region)

    try:
        client = boto3.client("cloudwatch", region_name=region)
        client.put_dashboard(DashboardName=name, DashboardBody=body)
        url = (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{name}"
        )
        print(f"✓ Bedrock usage dashboard created: {name}")
        print(f"  URL: {url}")
        return name
    except Exception as exc:
        print(f"Error creating Bedrock usage dashboard: {exc}")
        return None


def create_cloudwatch_dashboard(
    project_name: str,
    agent_runtime_arn: str,
    region: str,
) -> str | None:
    """Create or update the CloudWatch monitoring dashboard. Returns dashboard name."""
    if not agent_runtime_arn:
        print("Warning: agent_runtime_arn missing; skipping CloudWatch dashboard creation")
        return None

    name = dashboard_name(project_name)
    body = build_dashboard_body(project_name, agent_runtime_arn, region)

    try:
        client = boto3.client("cloudwatch", region_name=region)
        client.put_dashboard(DashboardName=name, DashboardBody=body)
        url = (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{name}"
        )
        print(f"✓ CloudWatch dashboard created: {name}")
        print(f"  URL: {url}")
        return name
    except Exception as exc:
        print(f"Error creating CloudWatch dashboard: {exc}")
        return None
