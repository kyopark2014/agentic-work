"""AgentCore Observability setup: Transaction Search and trace delivery."""

from __future__ import annotations

import json
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

SPANS_LOG_GROUP = "aws/spans"
RESOURCE_POLICY_NAME = "TransactionSearchXRayAccess"
DESTINATION_WAIT_SECONDS = 900
DESTINATION_POLL_INTERVAL = 15


def _need_resource_policy(logs_client) -> bool:
    try:
        response = logs_client.describe_resource_policies()
        for policy in response.get("resourcePolicies", []):
            if policy.get("policyName") == RESOURCE_POLICY_NAME:
                return False
        return True
    except Exception:
        return True


def _need_trace_destination(xray_client) -> bool:
    try:
        response = xray_client.get_trace_segment_destination()
        return response.get("Destination") != "CloudWatchLogs"
    except Exception:
        return True


def _need_indexing_rule(xray_client) -> bool:
    try:
        response = xray_client.get_indexing_rules()
        for rule in response.get("IndexingRules", []):
            if rule.get("Name") == "Default":
                return False
        return True
    except Exception:
        return True


def spans_log_group_exists(region: str) -> bool:
    logs_client = boto3.client("logs", region_name=region)
    response = logs_client.describe_log_groups(logGroupNamePrefix=SPANS_LOG_GROUP, limit=1)
    return any(group.get("logGroupName") == SPANS_LOG_GROUP for group in response.get("logGroups", []))


def _wait_for_trace_destination(xray_client, destination: str) -> str:
    deadline = time.time() + DESTINATION_WAIT_SECONDS
    while time.time() < deadline:
        response = xray_client.get_trace_segment_destination()
        status = response.get("Status", "UNKNOWN")
        current = response.get("Destination")
        if current == destination and status == "ACTIVE":
            return status
        time.sleep(DESTINATION_POLL_INTERVAL)
    response = xray_client.get_trace_segment_destination()
    return response.get("Status", "TIMEOUT")


def _create_cloudwatch_logs_resource_policy(logs_client, account_id: str, region: str) -> None:
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:aws/spans:*",
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/application-signals/data:*",
                ],
                "Condition": {
                    "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account_id}:*"},
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }
    logs_client.put_resource_policy(
        policyName=RESOURCE_POLICY_NAME,
        policyDocument=json.dumps(policy_document),
    )


def _configure_trace_segment_destination(xray_client) -> str:
    try:
        xray_client.update_trace_segment_destination(Destination="CloudWatchLogs")
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidRequestException":
            raise
    return _wait_for_trace_destination(xray_client, "CloudWatchLogs")


def _configure_indexing_rule(xray_client) -> None:
    try:
        xray_client.update_indexing_rule(
            Name="Default",
            Rule={"Probabilistic": {"DesiredSamplingPercentage": 1.0}},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidRequestException":
            raise


def _toggle_transaction_search_for_spans_log_group(region: str) -> str:
    xray_client = boto3.client("xray", region_name=region)
    print("  Toggling Transaction Search to create aws/spans log group...")
    xray_client.update_trace_segment_destination(Destination="XRay")
    xray_status = _wait_for_trace_destination(xray_client, "XRay")
    print(f"  X-Ray destination status: {xray_status}")
    xray_client.update_trace_segment_destination(Destination="CloudWatchLogs")
    cw_status = _wait_for_trace_destination(xray_client, "CloudWatchLogs")
    print(f"  CloudWatchLogs destination status: {cw_status}")
    return cw_status


def ensure_transaction_search(region: str, account_id: str) -> dict[str, Any]:
    """Enable Transaction Search prerequisites for AgentCore Observability."""
    result: dict[str, Any] = {"status": "success", "steps": []}
    logs_client = boto3.client("logs", region_name=region)
    xray_client = boto3.client("xray", region_name=region)

    if _need_resource_policy(logs_client):
        _create_cloudwatch_logs_resource_policy(logs_client, account_id, region)
        result["steps"].append("resource_policy")
    else:
        print("  CloudWatch Logs resource policy already configured")

    if _need_trace_destination(xray_client):
        status = _configure_trace_segment_destination(xray_client)
        result["steps"].append("trace_destination")
        result["destination_status"] = status
    else:
        response = xray_client.get_trace_segment_destination()
        result["destination_status"] = response.get("Status")
        print(f"  X-Ray trace destination already configured ({result['destination_status']})")

    if _need_indexing_rule(xray_client):
        _configure_indexing_rule(xray_client)
        result["steps"].append("indexing_rule")
    else:
        print("  X-Ray indexing rule already configured")

    if not spans_log_group_exists(region):
        print("  aws/spans log group not found; toggling Transaction Search")
        result["destination_status"] = _toggle_transaction_search_for_spans_log_group(region)
        result["steps"].append("spans_log_group_toggle")
    else:
        print("  aws/spans log group exists")

    try:
        observability_client = boto3.client("observabilityadmin", region_name=region)
        status = observability_client.get_telemetry_evaluation_status().get("Status")
        if status == "NOT_STARTED":
            observability_client.start_telemetry_evaluation()
            result["steps"].append("telemetry_evaluation_started")
            print("  Started CloudWatch telemetry evaluation")
    except Exception as error:
        result["telemetry_evaluation_warning"] = str(error)

    if not spans_log_group_exists(region):
        result["status"] = "pending"
        result["warning"] = (
            "aws/spans log group is still missing. OTEL trace exports may fail for up to "
            "10-15 minutes after Transaction Search becomes ACTIVE."
        )
    elif result.get("destination_status") not in (None, "ACTIVE"):
        result["status"] = "pending"
        result["warning"] = (
            "X-Ray trace destination is not ACTIVE yet. OTEL trace exports may fail for up to "
            "10-15 minutes."
        )

    return result


def _setup_traces_delivery(logs_client, resource_arn: str, resource_id: str, region: str, account_id: str) -> dict[str, str]:
    source_name = f"{resource_id}-traces-source"
    dest_name = f"{resource_id}-traces-destination"

    try:
        traces_source = logs_client.put_delivery_source(
            name=source_name,
            logType="TRACES",
            resourceArn=resource_arn,
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        traces_source = {"deliverySource": {"name": source_name}}

    try:
        traces_dest = logs_client.put_delivery_destination(
            name=dest_name,
            deliveryDestinationType="XRAY",
        )
        dest_arn = traces_dest["deliveryDestination"]["arn"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        dest_arn = f"arn:aws:logs:{region}:{account_id}:delivery-destination:{dest_name}"

    try:
        delivery = logs_client.create_delivery(
            deliverySourceName=traces_source["deliverySource"]["name"],
            deliveryDestinationArn=dest_arn,
        )
        delivery_id = delivery.get("id", "created")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConflictException":
            raise
        delivery_id = "existing"

    return {
        "delivery_id": delivery_id,
        "source_name": source_name,
        "destination_name": dest_name,
    }


def traces_delivery_configured(region: str, runtime_id: str) -> bool:
    logs_client = boto3.client("logs", region_name=region)
    try:
        logs_client.get_delivery_source(name=f"{runtime_id}-traces-source")
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def ensure_traces_delivery_for_runtime(runtime_arn: str, runtime_id: str, region: str, account_id: str) -> dict[str, Any]:
    """Configure CloudWatch TRACES delivery for an AgentCore runtime."""
    if traces_delivery_configured(region, runtime_id):
        print(f"  Trace delivery already configured for runtime {runtime_id}")
        return {"status": "success", "runtime_id": runtime_id, "configured": True}

    logs_client = boto3.client("logs", region_name=region)
    delivery = _setup_traces_delivery(logs_client, runtime_arn, runtime_id, region, account_id)
    print(f"  Trace delivery configured for runtime {runtime_id}")
    return {"status": "success", "runtime_id": runtime_id, "delivery": delivery}


def setup_agentcore_observability(runtime_arn: str | None, region: str, account_id: str) -> dict[str, Any]:
    """Enable Transaction Search and runtime trace delivery."""
    result: dict[str, Any] = {"transaction_search": {}, "traces_delivery": {}}

    print("  Enabling CloudWatch Transaction Search...")
    result["transaction_search"] = ensure_transaction_search(region, account_id)

    if not runtime_arn:
        result["status"] = "skipped"
        result["warning"] = "agent_runtime_arn not found; skipped trace delivery setup"
        return result

    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    print(f"  Enabling trace delivery for runtime {runtime_id}...")
    result["traces_delivery"] = ensure_traces_delivery_for_runtime(
        runtime_arn, runtime_id, region, account_id
    )
    result["status"] = "success"
    if result["transaction_search"].get("warning"):
        result["warning"] = result["transaction_search"]["warning"]
    return result
