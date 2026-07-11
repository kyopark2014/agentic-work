"""AgentCore Evaluations setup: execution role and online evaluation config."""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

DEFAULT_EVALUATORS = [
    "Builtin.Helpfulness",
    "Builtin.GoalSuccessRate",
    "Builtin.ToolSelectionAccuracy",
]
DEFAULT_SAMPLING_PERCENTAGE = 10.0
# Idle minutes before online evaluation treats a session as complete.
# Keep short so Chat-mode long-lived runtimeSessionId sessions stay under
# the 1000-span / 15 MB evaluation quota.
DEFAULT_SESSION_TIMEOUT_MINUTES = 5
# Agent runtime names previously used runtime_type (e.g. langgraph) instead of projectName.
LEGACY_AGENT_RUNTIME_TYPE = "langgraph"


def agent_runtime_name(project_name: str) -> str:
    """Return Bedrock AgentCore runtime name (e.g. agentic_work)."""
    return project_name.replace("-", "_")


def evaluation_role_name(project_name: str) -> str:
    return f"AmazonBedrockAgentCoreEvaluationRoleFor{project_name}"


def online_evaluation_config_name(project_name: str) -> str:
    safe_name = project_name.replace("-", "_")
    return f"{safe_name}_langgraph_online_eval"


def runtime_service_name(project_name: str, qualifier: str = "DEFAULT") -> str:
    return f"{agent_runtime_name(project_name)}.{qualifier}"


def runtime_traces_log_group(runtime_id: str, qualifier: str = "DEFAULT") -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{qualifier}"


def _evaluation_trust_policy(account_id: str, region: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TrustPolicyStatement",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id,
                        "aws:ResourceAccount": account_id,
                    },
                    "ArnLike": {
                        "aws:SourceArn": [
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:evaluator/*",
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:online-evaluation-config/*",
                        ]
                    },
                },
            },
        ],
    }


def _evaluation_permissions_policy(region: str, account_id: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CloudWatchLogReadStatement",
                "Effect": "Allow",
                "Action": [
                    "logs:DescribeLogGroups",
                    "logs:GetQueryResults",
                    "logs:StartQuery",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                ],
                "Resource": "*",
            },
            {
                "Sid": "CloudWatchLogWriteStatement",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": (
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/evaluations/*"
                ),
            },
            {
                "Sid": "CloudWatchIndexPolicyStatement",
                "Effect": "Allow",
                "Action": [
                    "logs:DescribeIndexPolicies",
                    "logs:PutIndexPolicy",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:aws/spans",
                    f"arn:aws:logs:{region}:{account_id}:log-group:aws/spans:*",
                ],
            },
            {
                "Sid": "BedrockInvokeStatement",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                ],
            },
        ],
    }


def _upsert_iam_policy(
    iam_client,
    policy_name: str,
    policy_document: dict[str, Any],
    account_id: str,
) -> str:
    policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
    document = json.dumps(policy_document)

    try:
        iam_client.get_policy(PolicyArn=policy_arn)
        versions = iam_client.list_policy_versions(PolicyArn=policy_arn)["Versions"]
        if len(versions) >= 5:
            for version in versions:
                if not version["IsDefaultVersion"]:
                    iam_client.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=version["VersionId"],
                    )
                    break
        iam_client.create_policy_version(
            PolicyArn=policy_arn,
            PolicyDocument=document,
            SetAsDefault=True,
        )
        print(f"  Updated IAM policy: {policy_name}")
    except iam_client.exceptions.NoSuchEntityException:
        response = iam_client.create_policy(
            PolicyName=policy_name,
            PolicyDocument=document,
            Description="Permissions for Amazon Bedrock AgentCore Evaluations",
        )
        policy_arn = response["Policy"]["Arn"]
        print(f"  Created IAM policy: {policy_name}")

    return policy_arn


def ensure_evaluation_execution_role(region: str, account_id: str, project_name: str) -> str:
    """Create or update the IAM role used by AgentCore Evaluations."""
    iam_client = boto3.client("iam")
    role_name = evaluation_role_name(project_name)
    policy_name = f"{role_name}Policy"

    policy_arn = _upsert_iam_policy(
        iam_client,
        policy_name,
        _evaluation_permissions_policy(region, account_id),
        account_id,
    )

    trust_policy = json.dumps(_evaluation_trust_policy(account_id, region))
    try:
        iam_client.get_role(RoleName=role_name)
        iam_client.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=trust_policy,
        )
        print(f"  Existing evaluation role found: {role_name}")
    except iam_client.exceptions.NoSuchEntityException:
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=trust_policy,
            Description="Execution role for Amazon Bedrock AgentCore Evaluations",
        )
        print(f"  Created evaluation role: {role_name}")

    iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return f"arn:aws:iam::{account_id}:role/{role_name}"


def _find_online_evaluation_config(client, config_name: str) -> dict[str, Any] | None:
    next_token = None
    while True:
        params: dict[str, Any] = {}
        if next_token:
            params["nextToken"] = next_token
        response = client.list_online_evaluation_configs(**params)
        for item in response.get("onlineEvaluationConfigs", []):
            if item.get("onlineEvaluationConfigName") == config_name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def _evaluation_rule(
    sampling_percentage: float,
    session_timeout_minutes: int,
) -> dict[str, Any]:
    return {
        "samplingConfig": {"samplingPercentage": sampling_percentage},
        "sessionConfig": {"sessionTimeoutMinutes": session_timeout_minutes},
    }


def _cloudwatch_data_source_config(
    log_group: str,
    service_name: str,
) -> dict[str, Any]:
    return {
        "cloudWatchLogs": {
            "logGroupNames": [log_group],
            "serviceNames": [service_name],
        }
    }


def _legacy_runtime_names(project_name: str) -> set[str]:
    """Return superseded AgentCore runtime names for this project."""
    safe_project = project_name.replace("-", "_")
    current_name = agent_runtime_name(project_name)
    stale = {
        f"runtime_{LEGACY_AGENT_RUNTIME_TYPE}",
        f"runtime_{safe_project}",
        f"{safe_project}_{LEGACY_AGENT_RUNTIME_TYPE}",
    }
    stale.discard(current_name)
    return stale


def cleanup_stale_agent_runtimes(
    region: str,
    active_runtime_arn: str | None,
    project_name: str,
) -> list[str]:
    """Delete legacy AgentCore runtimes that are no longer used by this project."""
    if not active_runtime_arn:
        return []

    active_id = active_runtime_arn.rsplit("/", 1)[-1]
    stale_names = _legacy_runtime_names(project_name)
    if not stale_names:
        return []

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    deleted: list[str] = []
    for runtime in client.list_agent_runtimes().get("agentRuntimes", []):
        runtime_id = runtime.get("agentRuntimeId", "")
        runtime_name = runtime.get("agentRuntimeName", "")
        if runtime_id == active_id or runtime_name not in stale_names:
            continue
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
            deleted.append(f"{runtime_name} ({runtime_id})")
            print(f"  Deleted stale agent runtime: {runtime_name} ({runtime_id})")
        except ClientError as error:
            print(f"  Warning: failed to delete stale runtime {runtime_name}: {error}")
    return deleted


def ensure_online_evaluation_config(
    runtime_arn: str,
    region: str,
    account_id: str,
    project_name: str,
    execution_role_arn: str,
    sampling_percentage: float = DEFAULT_SAMPLING_PERCENTAGE,
    session_timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MINUTES,
    evaluators: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update an online evaluation config for the LangGraph AgentCore runtime."""
    config_name = online_evaluation_config_name(project_name)
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    service_name = runtime_service_name(project_name)
    log_group = runtime_traces_log_group(runtime_id)
    evaluator_ids = evaluators or DEFAULT_EVALUATORS
    rule = _evaluation_rule(sampling_percentage, session_timeout_minutes)
    data_source_config = _cloudwatch_data_source_config(log_group, service_name)

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    existing = _find_online_evaluation_config(client, config_name)
    if existing:
        config_id = existing["onlineEvaluationConfigId"]
        client.update_online_evaluation_config(
            onlineEvaluationConfigId=config_id,
            rule=rule,
            dataSourceConfig=data_source_config,
            evaluationExecutionRoleArn=execution_role_arn,
            evaluators=[{"evaluatorId": evaluator_id} for evaluator_id in evaluator_ids],
        )
        print(
            f"  Updated online evaluation config: {config_name} "
            f"(sessionTimeoutMinutes={session_timeout_minutes}, "
            f"logGroup={log_group}, serviceName={service_name})"
        )
        return {
            "status": "success",
            "online_evaluation_config_name": config_name,
            "online_evaluation_config_id": config_id,
            "configured": True,
            "service_name": service_name,
            "log_group": log_group,
            "evaluators": evaluator_ids,
            "sampling_percentage": sampling_percentage,
            "session_timeout_minutes": session_timeout_minutes,
        }

    response = client.create_online_evaluation_config(
        onlineEvaluationConfigName=config_name,
        description=f"Online evaluation for LangGraph runtime ({project_name})",
        rule=rule,
        dataSourceConfig=data_source_config,
        evaluators=[{"evaluatorId": evaluator_id} for evaluator_id in evaluator_ids],
        evaluationExecutionRoleArn=execution_role_arn,
        enableOnCreate=True,
    )
    print(
        f"  Created online evaluation config: {config_name} "
        f"(sessionTimeoutMinutes={session_timeout_minutes})"
    )
    return {
        "status": "success",
        "online_evaluation_config_name": config_name,
        "online_evaluation_config_id": response.get("onlineEvaluationConfigId"),
        "configured": True,
        "service_name": service_name,
        "log_group": log_group,
        "evaluators": evaluator_ids,
        "sampling_percentage": sampling_percentage,
        "session_timeout_minutes": session_timeout_minutes,
    }


def setup_agentcore_evaluation(
    runtime_arn: str | None,
    region: str,
    account_id: str,
    project_name: str,
) -> dict[str, Any]:
    """Create evaluation IAM role and online evaluation configuration."""
    result: dict[str, Any] = {"status": "success"}

    if not runtime_arn:
        result["status"] = "skipped"
        result["warning"] = "agent_runtime_arn not found; skipped evaluation setup"
        return result

    print("  Creating evaluation execution role...")
    execution_role_arn = ensure_evaluation_execution_role(region, account_id, project_name)
    result["evaluation_execution_role_arn"] = execution_role_arn

    print("  Cleaning up stale agent runtimes...")
    deleted_runtimes = cleanup_stale_agent_runtimes(
        region=region,
        active_runtime_arn=runtime_arn,
        project_name=project_name,
    )
    if deleted_runtimes:
        result["deleted_stale_runtimes"] = deleted_runtimes

    print("  Creating online evaluation configuration...")
    evaluation = ensure_online_evaluation_config(
        runtime_arn=runtime_arn,
        region=region,
        account_id=account_id,
        project_name=project_name,
        execution_role_arn=execution_role_arn,
    )
    result.update(evaluation)
    return result
