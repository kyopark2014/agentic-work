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
    except Exception as exc:
        logger.warning("CloudWatch user metrics publish skipped: %s", exc)


def _search_metric(
    metric_name: str,
    project_name: str,
    period: int,
    *,
    stat: str = "Sum",
    label: str | None = None,
    metric_id: str | None = None,
) -> list[dict[str, Any]]:
    expr = (
        f"SEARCH('{{{METRIC_NAMESPACE},ProjectName,Method}} "
        f"MetricName=\"{metric_name}\" ProjectName=\"{project_name}\"', "
        f"'{stat}', {period})"
    )
    row: dict[str, Any] = {
        "expression": f"SUM({expr})",
        "label": label or metric_name,
    }
    if metric_id:
        row["id"] = metric_id
    return [row]


def _by_method_search(
    metric_name: str,
    project_name: str,
    period: int,
) -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "expression": (
                    f"SEARCH('{{{METRIC_NAMESPACE},ProjectName,Method}} "
                    f"MetricName=\"{metric_name}\" ProjectName=\"{project_name}\"', "
                    f"'Sum', {period})"
                ),
                "id": "m1",
            }
        ]
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
            "height": 2,
            "properties": {
                "markdown": (
                    f"# {name}\n"
                    f"**Region** `{region}` · **Namespace** `{METRIC_NAMESPACE}` · "
                    f"**Project** `{project_name}`\n\n"
                    "애플리케이션 **가입(NewUsers) · 접속(UserLogins)** 현황. "
                    "Google/로컬 로그인 시 ECS 앱이 PutMetricData로 발행합니다."
                )
            },
        }
    )
    y += 2

    kpi_specs = [
        (0, "Logins (24h)", "UserLogins", 86400),
        (6, "New Users (24h)", "NewUsers", 86400),
        (12, "Logins (7d)", "UserLogins", 604800),
        (18, "New Users (7d)", "NewUsers", 604800),
    ]
    for x, title, metric, period in kpi_specs:
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
                    "period": period,
                    "stat": "Sum",
                    "sparkline": True,
                    "setPeriodToTimeRange": True,
                    # CloudWatch expects metrics as an array of metric arrays.
                    "metrics": [_search_metric(metric, project_name, period)],
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
                "title": "User Logins",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 3600,
                "stat": "Sum",
                "yAxis": {"left": {"label": "Count", "showUnits": False}},
                "metrics": _by_method_search("UserLogins", project_name, 3600),
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
                "title": "New Users",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 3600,
                "stat": "Sum",
                "yAxis": {"left": {"label": "Count", "showUnits": False}},
                "metrics": _by_method_search("NewUsers", project_name, 3600),
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
            "properties": {"markdown": "### By auth method"},
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
                "title": "Logins by Method (24h)",
                "view": "pie",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "setPeriodToTimeRange": True,
                "labels": {"visible": True},
                "legend": {"position": "bottom"},
                "metrics": _by_method_search("UserLogins", project_name, 86400),
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
                "title": "New Users by Method (24h)",
                "view": "pie",
                "region": region,
                "period": 86400,
                "stat": "Sum",
                "setPeriodToTimeRange": True,
                "labels": {"visible": True},
                "legend": {"position": "bottom"},
                "metrics": _by_method_search("NewUsers", project_name, 86400),
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
