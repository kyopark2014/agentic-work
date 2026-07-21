"""CloudWatch metrics + dashboard for application user access (logins / signups)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

METRIC_NAMESPACE = "AgenticWork/Application"
DASHBOARD_SUFFIX = "user-access"


def user_access_dashboard_name(project_name: str) -> str:
    return f"{project_name}-{DASHBOARD_SUFFIX}"


def _load_context() -> dict[str, str]:
    try:
        from application import utils

        cfg = utils.load_config()
    except Exception:
        try:
            import utils  # type: ignore

            cfg = utils.load_config()
        except Exception:
            cfg = {}
    project = str(cfg.get("projectName") or "agentic-work").strip() or "agentic-work"
    region = str(cfg.get("region") or "us-west-2").strip() or "us-west-2"
    return {"ProjectName": project, "region": region}


def publish_login_metrics(
    *,
    method: str,
    is_new_user: bool = False,
) -> None:
    """Publish login (and optional new-user) counts. Never raises to callers."""
    try:
        context = _load_context()
        project = context["ProjectName"]
        region = context["region"]
        method_value = (method or "unknown").strip().lower() or "unknown"
        dimensions = [
            {"Name": "ProjectName", "Value": project},
            {"Name": "Method", "Value": method_value},
        ]
        now = datetime.now(timezone.utc)
        metric_data: list[dict[str, Any]] = [
            {
                "MetricName": "UserLogins",
                "Dimensions": dimensions,
                "Timestamp": now,
                "Value": 1.0,
                "Unit": "Count",
            },
        ]
        if is_new_user:
            metric_data.append(
                {
                    "MetricName": "NewUsers",
                    "Dimensions": dimensions,
                    "Timestamp": now,
                    "Value": 1.0,
                    "Unit": "Count",
                }
            )
        client = boto3.client("cloudwatch", region_name=region)
        client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data)
        logger.info(
            "Published CloudWatch %s metrics method=%s new_user=%s",
            METRIC_NAMESPACE,
            method_value,
            is_new_user,
        )
    except Exception as exc:
        logger.warning("CloudWatch user metrics publish skipped: %s", exc)


def _direct_metric(
    metric_name: str,
    project_name: str,
    method: str,
    *,
    metric_id: str,
    visible: bool = True,
    period: int = 300,
    stat: str = "Sum",
) -> list[Any]:
    """CloudWatch console-friendly metric array with explicit dimensions."""
    opts: dict[str, Any] = {
        "id": metric_id,
        "period": period,
        "stat": stat,
    }
    if not visible:
        opts["visible"] = False
    return [
        METRIC_NAMESPACE,
        metric_name,
        "ProjectName",
        project_name,
        "Method",
        method,
        opts,
    ]


def _sum_methods_expression(
    metric_name: str,
    project_name: str,
    *,
    label: str,
    expression_id: str = "e1",
    period: int = 300,
) -> list[list[Any]]:
    """Sum google + local (+ probe) without SEARCH, for reliable KPI widgets."""
    return [
        _direct_metric(
            metric_name, project_name, "google", metric_id="m1", visible=False, period=period
        ),
        _direct_metric(
            metric_name, project_name, "local", metric_id="m2", visible=False, period=period
        ),
        [
            {
                "expression": "IF(m1, m1, 0) + IF(m2, m2, 0)",
                "label": label,
                "id": expression_id,
            }
        ],
    ]


def _by_method_metrics(
    metric_name: str,
    project_name: str,
    *,
    period: int = 300,
) -> list[list[Any]]:
    return [
        _direct_metric(
            metric_name, project_name, "google", metric_id="m1", period=period
        ),
        _direct_metric(
            metric_name, project_name, "local", metric_id="m2", period=period
        ),
    ]


def build_user_access_dashboard_body(project_name: str, region: str) -> str:
    """Build CloudWatch dashboard JSON for user access metrics."""
    name = user_access_dashboard_name(project_name)
    widgets: list[dict[str, Any]] = []
    y = 0

    widgets.append(
        {
            "type": "text",
            "x": 0,
            "y": y,
            "width": 24,
            "height": 3,
            "properties": {
                "markdown": (
                    f"# {name}\n"
                    f"**Region** `{region}` · **Namespace** `{METRIC_NAMESPACE}` · "
                    f"**Project** `{project_name}`\n\n"
                    "애플리케이션 **가입(NewUsers) · 접속(UserLogins)** 현황. "
                    "Google/로컬 로그인 시 ECS 앱이 `PutMetricData`로 발행합니다.\n\n"
                    f"> 콘솔에서 반드시 **{region}** 리전으로 여세요. "
                    "상단 시간 범위를 `3h` / `1d` / `1w`로 바꿔가며 확인하세요."
                )
            },
        }
    )
    y += 3

    # Use a short metric period; dashboard time range controls the window.
    kpi_specs = [
        (0, "Logins (window)", "UserLogins"),
        (6, "New Users (window)", "NewUsers"),
        (12, "Google Logins", "UserLogins"),
        (18, "Google New Users", "NewUsers"),
    ]
    for idx, (x, title, metric) in enumerate(kpi_specs):
        if idx < 2:
            metrics = _sum_methods_expression(metric, project_name, label=metric)
        else:
            metrics = [
                _direct_metric(
                    metric, project_name, "google", metric_id="m1", period=300
                )
            ]
        widgets.append(
            {
                "type": "metric",
                "x": x,
                "y": y,
                "width": 6,
                "height": 4,
                "properties": {
                    "title": title,
                    "view": "singleValue",
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "sparkline": True,
                    "setPeriodToTimeRange": True,
                    "metrics": metrics,
                },
            }
        )
    y += 4

    widgets.append(
        {
            "type": "text",
            "x": 0,
            "y": y,
            "width": 24,
            "height": 1,
            "properties": {"markdown": "### Access over time"},
        }
    )
    y += 1

    widgets.append(
        {
            "type": "metric",
            "x": 0,
            "y": y,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "User Logins by Method",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "yAxis": {"left": {"label": "Count", "showUnits": False}},
                "metrics": _by_method_metrics("UserLogins", project_name, period=300),
            },
        }
    )
    widgets.append(
        {
            "type": "metric",
            "x": 12,
            "y": y,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "New Users by Method",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 300,
                "stat": "Sum",
                "yAxis": {"left": {"label": "Count", "showUnits": False}},
                "metrics": _by_method_metrics("NewUsers", project_name, period=300),
            },
        }
    )
    y += 6

    widgets.append(
        {
            "type": "text",
            "x": 0,
            "y": y,
            "width": 24,
            "height": 1,
            "properties": {
                "markdown": "### By auth method (current dashboard time range)"
            },
        }
    )
    y += 1

    widgets.append(
        {
            "type": "metric",
            "x": 0,
            "y": y,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Logins by Method",
                "view": "pie",
                "region": region,
                "period": 300,
                "stat": "Sum",
                "setPeriodToTimeRange": True,
                "labels": {"visible": True},
                "legend": {"position": "bottom"},
                "metrics": _by_method_metrics("UserLogins", project_name, period=300),
            },
        }
    )
    widgets.append(
        {
            "type": "metric",
            "x": 12,
            "y": y,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "New Users by Method",
                "view": "pie",
                "region": region,
                "period": 300,
                "stat": "Sum",
                "setPeriodToTimeRange": True,
                "labels": {"visible": True},
                "legend": {"position": "bottom"},
                "metrics": _by_method_metrics("NewUsers", project_name, period=300),
            },
        }
    )

    return json.dumps({"widgets": widgets})


def create_user_access_dashboard(
    project_name: str | None = None,
    region: str | None = None,
) -> str | None:
    """Create or update the user-access CloudWatch dashboard. Returns name."""
    context = _load_context()
    project = (project_name or context["ProjectName"]).strip()
    region_name = (region or context["region"]).strip()
    name = user_access_dashboard_name(project)
    body = build_user_access_dashboard_body(project, region_name)

    try:
        client = boto3.client("cloudwatch", region_name=region_name)
        client.put_dashboard(DashboardName=name, DashboardBody=body)
        url = (
            f"https://{region_name}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region_name}#dashboards/dashboard/{name}"
        )
        logger.info("CloudWatch user-access dashboard ready: %s (%s)", name, url)
        return name
    except Exception as exc:
        logger.error("Failed to create user-access CloudWatch dashboard: %s", exc)
        return None
