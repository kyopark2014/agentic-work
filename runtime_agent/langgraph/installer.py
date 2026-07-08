#!/usr/bin/env python3
"""
Unified installation script
Sequentially executes: IAM policy creation -> Docker image build and ECR push -> AgentCore runtime creation/update
All functionality integrated into a single file
"""

import subprocess
import sys
import os
import json
import shutil
from datetime import datetime
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")

def load_config():
    """Load config.json file and merge S3 Files settings from application config."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"Failed to parse config.json file: {e}")
        config = {}
        config["region"] = boto3.Session().region_name or "us-west-2"
        config["projectName"] = "langgraph-runtime"

        sts = boto3.client("sts")
        response = sts.get_caller_identity()
        config["accountId"] = response["Account"]

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return _merge_application_config(config)

    if not config.get("region"):
        config["region"] = boto3.Session().region_name or "us-west-2"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    return _merge_application_config(config)


def _merge_application_config(config: dict) -> dict:
    """Pull S3 Files session storage keys from application/config.json."""
    app_config = _load_application_config()
    updated = dict(config)
    changed = False
    for key in (
        "s3_files_access_point_arn",
        "s3_files_file_system_id",
        "agent_runtime_vpc_subnets",
        "agent_runtime_security_groups",
    ):
        app_value = app_config.get(key)
        if app_value and updated.get(key) != app_value:
            updated[key] = app_value
            changed = True
    if changed:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2)
    return updated

def update_config(key, value):
    """Update config.json with a key-value pair."""
    try:
        config = load_config()
        config[key] = value
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"Error updating config: {e}")
        return False


def _repo_root() -> str:
    return os.path.normpath(os.path.join(script_dir, "..", ".."))


def get_knowledge_base_name() -> str:
    """Return project_name from repo root installer.py (Bedrock Knowledge Base name)."""
    root_installer_path = os.path.join(_repo_root(), "installer.py")
    try:
        with open(root_installer_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("project_name = "):
                    value = stripped.split("=", 1)[1].strip()
                    if "#" in value:
                        value = value.split("#", 1)[0].strip()
                    return value.strip('"').strip("'")
    except OSError as e:
        print(f"Warning: Could not read root installer.py: {e}")

    app_config_path = os.path.join(_repo_root(), "application", "config.json")
    try:
        with open(app_config_path, "r", encoding="utf-8") as f:
            app_config = json.load(f)
            project_name = app_config.get("projectName")
            if project_name:
                return project_name
    except (OSError, json.JSONDecodeError):
        pass

    return "langgraph-runtime"


def _load_application_config() -> dict:
    """Load application/config.json when available."""
    app_config_path = os.path.join(_repo_root(), "application", "config.json")
    try:
        with open(app_config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_s3_vectors_names(vector_bucket_arn: str, index_arn: str) -> dict:
    """Derive vector bucket/index names from S3 Vectors ARNs."""
    vector_bucket_name = ""
    vector_index_name = ""
    if vector_bucket_arn and "/bucket/" in vector_bucket_arn:
        vector_bucket_name = vector_bucket_arn.split("/bucket/", 1)[1].split("/")[0]
    if index_arn and "/index/" in index_arn:
        vector_index_name = index_arn.rsplit("/index/", 1)[1]
    return {
        "vector_bucket_name": vector_bucket_name,
        "vector_index_name": vector_index_name,
    }


def _find_data_source_id(bedrock_agent_client, knowledge_base_id: str, s3_bucket_name: str = "") -> str:
    """Return matching data source ID for the Knowledge Base."""
    try:
        response = bedrock_agent_client.list_data_sources(
            knowledgeBaseId=knowledge_base_id,
            maxResults=100,
        )
        data_sources = response.get("dataSourceSummaries", [])
        if not data_sources:
            return ""

        if s3_bucket_name:
            for data_source in data_sources:
                if data_source.get("name") == s3_bucket_name:
                    return data_source.get("dataSourceId", "")

        return data_sources[0].get("dataSourceId", "")
    except ClientError as e:
        print(f"Warning: Could not list data sources: {e}")
        return ""


def update_knowledge_base_config() -> bool:
    """Look up Knowledge Base by root installer project_name and update config.json."""
    print(f"\n{'='*60}")
    print("Updating Knowledge Base configuration")
    print(f"{'='*60}")

    try:
        config = load_config()
        region = config.get("region")
        if not region:
            print("Error: region not found in config.json")
            return False

        knowledge_base_name = get_knowledge_base_name()
        print(f"Knowledge Base name (from root installer.py): {knowledge_base_name}")

        app_config = _load_application_config()
        bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
        kb_list = bedrock_agent_client.list_knowledge_bases()
        knowledge_base_id = None
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb.get("name") == knowledge_base_name:
                knowledge_base_id = kb.get("knowledgeBaseId")
                break

        if not knowledge_base_id:
            print(f"Warning: Knowledge Base '{knowledge_base_name}' not found in {region}")
            updates = {
                "knowledge_base_name": knowledge_base_name,
                "collectionArn": "",
                "opensearch_url": "",
            }
            for key in (
                "knowledge_base_id",
                "knowledge_base_role",
                "data_source_id",
                "s3_bucket",
                "s3_arn",
                "sharing_url",
                "vector_bucket_name",
                "vector_bucket_arn",
                "vector_index_name",
                "vector_index_arn",
                "s3_files_access_point_arn",
                "s3_files_file_system_id",
                "agent_runtime_vpc_subnets",
                "agent_runtime_security_groups",
            ):
                if app_config.get(key):
                    updates[key] = app_config[key]
            config.update(updates)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            if updates.get("knowledge_base_id"):
                print(f"✓ config.json updated from application/config.json")
                print(f"  - knowledge_base_id: {updates['knowledge_base_id']}")
            else:
                print("✓ config.json updated with knowledge_base_name only")
            return True

        kb_details = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
        knowledge_base = kb_details.get("knowledgeBase", {})

        updates = {
            "knowledge_base_name": knowledge_base_name,
            "knowledge_base_id": knowledge_base_id,
            "knowledge_base_role": knowledge_base.get("roleArn", ""),
            "collectionArn": "",
            "opensearch_url": "",
        }
 
        storage = knowledge_base.get("storageConfiguration", {})
        storage_type = storage.get("type", "")
        if storage_type == "S3_VECTORS":
            s3_vectors_cfg = storage.get("s3VectorsConfiguration", {})
            vector_bucket_arn = s3_vectors_cfg.get("vectorBucketArn", "")
            vector_index_arn = s3_vectors_cfg.get("indexArn", "")
            parsed_names = _parse_s3_vectors_names(vector_bucket_arn, vector_index_arn)
            updates.update(
                {
                    "vector_bucket_arn": vector_bucket_arn,
                    "vector_index_arn": vector_index_arn,
                    "vector_bucket_name": parsed_names["vector_bucket_name"],
                    "vector_index_name": parsed_names["vector_index_name"],
                }
            )
        elif storage_type == "OPENSEARCH_SERVERLESS":
            opensearch_cfg = storage.get("opensearchServerlessConfiguration", {})
            updates["collectionArn"] = opensearch_cfg.get("collectionArn", "")
            updates["vector_bucket_name"] = ""
            updates["vector_bucket_arn"] = ""
            updates["vector_index_name"] = ""
            updates["vector_index_arn"] = ""

        for key in (
            "s3_bucket",
            "s3_arn",
            "sharing_url",
            "data_source_id",
            "vector_bucket_name",
            "vector_bucket_arn",
            "vector_index_name",
            "vector_index_arn",
            "s3_files_access_point_arn",
            "s3_files_file_system_id",
            "agent_runtime_vpc_subnets",
            "agent_runtime_security_groups",
        ):
            if app_config.get(key) and not updates.get(key):
                updates[key] = app_config[key]

        s3_bucket_name = updates.get("s3_bucket") or app_config.get("s3_bucket", "")
        if not updates.get("data_source_id"):
            updates["data_source_id"] = _find_data_source_id(
                bedrock_agent_client,
                knowledge_base_id,
                s3_bucket_name,
            )

        config.update(updates)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        print(f"✓ config.json updated for Knowledge Base: {knowledge_base_name}")
        print(f"  - knowledge_base_id: {updates.get('knowledge_base_id', '')}")
        if updates.get("data_source_id"):
            print(f"  - data_source_id: {updates['data_source_id']}")
        if updates.get("vector_index_arn"):
            print(f"  - vector_index_arn: {updates['vector_index_arn']}")
        return True
    except Exception as e:
        print(f"Error updating Knowledge Base configuration: {e}")
        return False

# ============================================================================
# IAM Policy and Role Creation Functions
# ============================================================================

def create_bedrock_agentcore_policy(config):
    """Create IAM policy for Bedrock AgentCore access"""
    region = config['region']
    accountId = config['accountId']
    projectName = config.get('projectName', 'agentcore')
    
    policy_name = f"AmazonBedrockAgentCoreRuntimePolicyFor{projectName}"
    policy_description = f"Policy for accessing Bedrock AgentCore Runtime endpoints"
    
    # Comprehensive policy document for Bedrock AgentCore access
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockAgentAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:*"
                ],
                "Resource": [
                    "*"
                ]
            },
            {
                "Sid": "BedrockMantleAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock-mantle:Get*",
                    "bedrock-mantle:List*",
                    "bedrock-mantle:CreateInference"
                ],
                "Resource": [
                    f"arn:aws:bedrock-mantle:{region}:{accountId}:project/*"
                ]
            },
            {
                "Sid": "BedrockMantleBearerToken",
                "Effect": "Allow",
                "Action": [
                    "bedrock-mantle:CallWithBearerToken"
                ],
                "Resource": "*"
            },
            {
                "Sid": "SecretsManagerAccess",
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:UpdateSecret",
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue"
                ],
                "Resource": [
                    f"arn:aws:secretsmanager:{region}:*:secret:{projectName}/cognito/credentials*",
                    f"arn:aws:secretsmanager:{region}:*:secret:{projectName}/credentials*",
                ]
            },
            {
                "Sid": "CognitoAccess",
                "Effect": "Allow",
                "Action": [
                    "cognito-idp:*"
                ],
                "Resource": "*"
            },
            {
                "Sid": "ECRAccess",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:DescribeRepositories",
                    "ecr:ListImages",
                    "ecr:DescribeImages"
                ],
                "Resource": "*"
            },
            {
                "Sid": "LogsAccess",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams"
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:*:log-group:/aws/bedrock-agentcore/*",
                    f"arn:aws:logs:{region}:*:log-group:/aws/bedrock-agentcore/*:log-stream:*"
                ]
            },
            {
                "Sid": "CloudWatchAccess",
                "Effect": "Allow",
                "Action": [
                    'cloudwatch:ListMetrics',
                    'cloudwatch:GetMetricData',
                    'cloudwatch:GetMetricStatistics',
                    'cloudwatch:GetMetricWidgetImage',
                    'cloudwatch:PutMetricData',
                    'xray:PutTraceSegments',
                    'xray:PutTelemetryRecords',
                    'xray:PutAttributes',
                    'xray:GetTraceSummaries',
                    'logs:CreateLogGroup',
                    'logs:DescribeLogStreams', 
                    'logs:DescribeLogGroups', 
                    'logs:CreateLogStream', 
                    'logs:PutLogEvents'
                ],
                "Resource": "*"
            },
            {
                "Sid": "S3Access",
                "Effect": "Allow",
                "Action": [
                    "s3:*",
                    "bedrock:*"
                ],
                "Resource": "*"
            },
            {
                "Sid": "EC2Access",
                "Effect": "Allow",
                "Action": [
                    "ec2:*"
                ],
                "Resource": "*"
            }
        ]
    }

    file_system_id = config.get("s3_files_file_system_id")
    access_point_arn = config.get("s3_files_access_point_arn")
    if file_system_id and access_point_arn:
        file_system_arn = (
            f"arn:aws:s3files:{region}:{accountId}:file-system/{file_system_id}"
        )
        policy_document["Statement"].extend(
            [
                {
                    "Sid": "S3FilesClientAccess",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:ClientMount",
                        "s3files:ClientWrite",
                        "s3files:ClientRootAccess",
                    ],
                    "Resource": file_system_arn,
                    "Condition": {
                        "ArnEquals": {
                            "s3files:AccessPointArn": access_point_arn,
                        }
                    },
                },
                {
                    "Sid": "S3FilesGetAccessPoint",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:GetAccessPoint",
                    ],
                    "Resource": access_point_arn,
                },
                {
                    "Sid": "S3FilesListMountTargets",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:ListMountTargets",
                    ],
                    "Resource": file_system_arn,
                },
            ]
        )
    
    try:
        iam_client = boto3.client('iam')
        
        # Check if policy already exists
        try:
            existing_policy = iam_client.get_policy(PolicyArn=f"arn:aws:iam::{accountId}:policy/{policy_name}")
            print(f"Existing policy found: {existing_policy['Policy']['Arn']}")
            
            # List all policy versions
            versions_response = iam_client.list_policy_versions(PolicyArn=existing_policy['Policy']['Arn'])
            versions = versions_response['Versions']
            
            # If we have 5 versions, delete the oldest non-default version
            if len(versions) >= 5:
                print(f"Policy has {len(versions)} versions, cleaning up old versions...")
                
                # Find non-default versions to delete
                non_default_versions = [v for v in versions if not v['IsDefaultVersion']]
                
                if non_default_versions:
                    # Delete the oldest non-default version
                    oldest_version = non_default_versions[0]
                    iam_client.delete_policy_version(
                        PolicyArn=existing_policy['Policy']['Arn'],
                        VersionId=oldest_version['VersionId']
                    )
                    print(f"✓ Deleted old policy version: {oldest_version['VersionId']}")
                else:
                    # If all versions are default, we need to set a different version as default first
                    for version in versions[1:]:  # Skip the current default
                        try:
                            iam_client.set_default_policy_version(
                                PolicyArn=existing_policy['Policy']['Arn'],
                                VersionId=version['VersionId']
                            )
                            # Now delete the old default
                            iam_client.delete_policy_version(
                                PolicyArn=existing_policy['Policy']['Arn'],
                                VersionId=versions[0]['VersionId']
                            )
                            print(f"✓ Switched default version and deleted old version: {versions[0]['VersionId']}")
                            break
                        except Exception as e:
                            print(f"Failed to switch version {version['VersionId']}: {e}")
                            continue
            
            # Create policy version
            response = iam_client.create_policy_version(
                PolicyArn=existing_policy['Policy']['Arn'],
                PolicyDocument=json.dumps(policy_document),
                SetAsDefault=True
            )
            print(f"✓ Policy update completed: {response['PolicyVersion']['VersionId']}")
            return existing_policy['Policy']['Arn']
            
        except iam_client.exceptions.NoSuchEntityException:
            # Create new policy
            response = iam_client.create_policy(
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_document),
                Description=policy_description
            )
            print(f"✓ New policy created: {response['Policy']['Arn']}")
            return response['Policy']['Arn']
            
    except Exception as e:
        print(f"Policy creation failed: {e}")
        return None

def attach_policy_to_role(role_name, policy_arn):
    """Attach policy to IAM role"""
    try:
        iam_client = boto3.client('iam')
        
        # Attach policy to role
        response = iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn=policy_arn
        )
        print(f"✓ Policy attached successfully: {policy_arn}")
        return True
        
    except Exception as e:
        print(f"Policy attachment failed: {e}")
        return False

def create_trust_policy_for_bedrock(config):
    """Create trust policy for Bedrock AgentCore"""
    accountId = config['accountId']
    
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            },
            {
                "Effect": "Allow",
                "Principal": {
                    "AWS": f"arn:aws:iam::{accountId}:root"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    return trust_policy

def create_bedrock_agentcore_role(config):
    """Create IAM role for Bedrock AgentCore MCP access"""
    projectName = config.get('projectName', 'agentcore')
    role_name = f"AmazonBedrockAgentCoreRuntimeRoleFor{projectName}"
    policy_arn = create_bedrock_agentcore_policy(config)
    
    if not policy_arn:
        print("Role creation aborted due to policy creation failure")
        return None
    
    try:
        iam_client = boto3.client('iam')
        
        # Check if role already exists
        try:
            existing_role = iam_client.get_role(RoleName=role_name)
            print(f"Existing role found: {existing_role['Role']['Arn']}")
            
            # Update trust policy
            trust_policy = create_trust_policy_for_bedrock(config)
            iam_client.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(trust_policy)
            )
            print("✓ Trust policy updated successfully")
            
            # Attach policy
            attach_policy_to_role(role_name, policy_arn)
            
            return existing_role['Role']['Arn']
            
        except iam_client.exceptions.NoSuchEntityException:
            # Create new role
            trust_policy = create_trust_policy_for_bedrock(config)
            
            response = iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Role for Bedrock AgentCore MCP access"
            )
            print(f"✓ New role created: {response['Role']['Arn']}")
            
            # Attach policy
            attach_policy_to_role(role_name, policy_arn)
            
            return response['Role']['Arn']
            
    except Exception as e:
        print(f"Role creation failed: {e}")
        return None

def create_iam_policies():
    """Create IAM policies and roles"""
    print(f"\n{'='*60}")
    print("Creating IAM policies and roles")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        
        # Create Bedrock AgentCore policy
        print("\n1. Creating Bedrock AgentCore policy...")
        policy_arn = create_bedrock_agentcore_policy(config)
        
        # Create Bedrock AgentCore role
        print("\n2. Creating Bedrock AgentCore role...")
        role_arn = create_bedrock_agentcore_role(config)
        
        if not role_arn:
            print("Role creation failed")
            return False
        
        # Update AgentCore configuration
        print("\n3. Updating AgentCore configuration...")
        update_config('agent_runtime_role', role_arn)
        print(f"✓ AgentCore configuration updated: {role_arn}")
        
        print("\n✓ IAM policies and roles creation completed")
        return True
        
    except Exception as e:
        print(f"Error creating IAM policies: {e}")
        return False

# ============================================================================
# Docker Build and ECR Push Functions
# ============================================================================

def check_aws_cli():
    """Check if AWS CLI is installed."""
    if not shutil.which("aws"):
        print("Error: AWS CLI is not installed")
        return False
    return True

def check_aws_credentials():
    """Check AWS credentials."""
    try:
        sts = boto3.client("sts")
        sts.get_caller_identity()
        return True
    except NoCredentialsError:
        print("Error: AWS credentials are not configured properly")
        return False
    except Exception as e:
        print(f"Error: Failed to verify AWS credentials: {e}")
        return False

DOCKER_MIN_FREE_MB = 2048
DOCKER_REQUIRED_FREE_MB = 1024


def ensure_ecr_repository(ecr_client, repository_name, region):
    """Check if ECR repository exists, create if it doesn't."""
    try:
        ecr_client.describe_repositories(repositoryNames=[repository_name])
        print(f"Repository {repository_name} exists.")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'RepositoryNotFoundException':
            print(f"Repository {repository_name} does not exist. Creating it...")
            try:
                ecr_client.create_repository(repositoryName=repository_name)
                print(f"Repository {repository_name} created successfully.")
                return True
            except Exception as create_error:
                print(f"Error: Failed to create repository: {create_error}")
                return False
        else:
            print(f"Error: Failed to check repository: {e}")
            return False


def _filesystem_free_mb(path: str) -> int:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1


def _docker_data_root() -> str:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "/var/lib/docker"


def _print_disk_usage(label: str, path: str) -> int:
    free_mb = _filesystem_free_mb(path)
    if free_mb >= 0:
        print(f"  {label}: {free_mb} MB free ({path})", flush=True)
    return free_mb


def cleanup_docker_resources() -> None:
    """Remove unused Docker data to reclaim disk space before build."""
    print("===== Cleaning Up Docker Resources =====", flush=True)
    for cmd, label in [
        (["docker", "builder", "prune", "-af"], "BuildKit cache"),
        (["docker", "image", "prune", "-af"], "Unused images"),
        (["docker", "container", "prune", "-f"], "Stopped containers"),
        (["docker", "volume", "prune", "-f"], "Unused volumes"),
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode == 0:
                output = (result.stdout or result.stderr).strip().splitlines()
                detail = output[-1] if output else "done"
                print(f"  ✓ Pruned {label}: {detail}", flush=True)
            else:
                err = (result.stderr or result.stdout).strip()
                print(f"  Warning: Failed to prune {label}: {err}", flush=True)
        except Exception as e:
            print(f"  Warning: Failed to prune {label}: {e}", flush=True)


def ensure_docker_disk_space(min_free_mb: int = DOCKER_MIN_FREE_MB) -> bool:
    """Ensure enough free disk space for Docker build; prune if needed."""
    print("===== Checking Docker Disk Space =====", flush=True)
    docker_root = _docker_data_root()
    root_free = _print_disk_usage("Root filesystem", "/")
    docker_free = _print_disk_usage("Docker data directory", docker_root)
    free_mb = min(root_free, docker_free) if root_free >= 0 and docker_free >= 0 else max(root_free, docker_free)

    if free_mb >= min_free_mb:
        print(f"  ✓ Sufficient disk space ({free_mb} MB >= {min_free_mb} MB)", flush=True)
        return True

    print(
        f"  Warning: Low disk space ({free_mb} MB free, need ~{min_free_mb} MB). "
        "Attempting Docker cleanup...",
        flush=True,
    )
    cleanup_docker_resources()

    root_free = _print_disk_usage("Root filesystem after cleanup", "/")
    docker_free = _print_disk_usage("Docker data directory after cleanup", docker_root)
    free_mb = min(root_free, docker_free) if root_free >= 0 and docker_free >= 0 else max(root_free, docker_free)

    if free_mb >= DOCKER_REQUIRED_FREE_MB:
        print(f"  ✓ Disk space available after cleanup ({free_mb} MB)", flush=True)
        return True

    print("Error: Not enough disk space for Docker build.", flush=True)
    print("  CloudShell and similar environments have limited storage.", flush=True)
    print("  Manual cleanup options:", flush=True)
    print("    docker system prune -af", flush=True)
    print("    rm -rf ~/.cache/pip ~/.npm /tmp/*", flush=True)
    print("  Or build elsewhere and rerun the root installer with --skip-docker-build.", flush=True)
    return False


def remove_local_docker_images(image_refs: list[str]) -> None:
    """Delete local image tags after a successful push to free disk space."""
    unique_refs = []
    for ref in image_refs:
        if ref and ref not in unique_refs:
            unique_refs.append(ref)
    if not unique_refs:
        return

    print("===== Removing Local Docker Images =====", flush=True)
    subprocess.run(
        ["docker", "rmi", "-f", *unique_refs],
        capture_output=True,
        text=True,
        check=False,
    )
    cleanup_docker_resources()


def check_docker_daemon(timeout: int = 30) -> bool:
    """Verify Docker daemon is reachable before build/push."""
    print("===== Checking Docker Daemon =====", flush=True)
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            print(f"Error: Docker daemon is not available: {result.stderr.strip()}")
            return False
        print("  ✓ Docker daemon is running", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print(f"Error: Docker daemon did not respond within {timeout}s.")
        print("  Start Docker Desktop and retry.")
        return False
    except Exception as e:
        print(f"Error: Failed to check Docker daemon: {e}")
        return False

def docker_login(account_id, region):
    """Login to AWS ECR using Docker."""
    ecr_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    try:
        print(f"  Logging in to {ecr_registry}...", flush=True)
        login_result = subprocess.run(
            ["aws", "ecr", "get-login-password", "--region", region],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if login_result.returncode != 0:
            print(f"Error: Failed to get ECR login password: {login_result.stderr.strip()}")
            return False

        docker_login_result = subprocess.run(
            ["docker", "login", "--username", "AWS", "--password-stdin", ecr_registry],
            input=login_result.stdout,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if docker_login_result.returncode != 0:
            err = docker_login_result.stderr.strip() or docker_login_result.stdout.strip()
            print(f"Error: Docker login failed: {err}")
            return False

        login_msg = docker_login_result.stdout.strip() or docker_login_result.stderr.strip()
        if login_msg:
            print(f"  {login_msg}", flush=True)
        print("  ✓ ECR login successful", flush=True)
        return True

    except subprocess.TimeoutExpired:
        print("Error: Docker login timed out. Ensure Docker Desktop is running and responsive.")
        return False
    except Exception as e:
        print(f"Error: Failed to login to ECR: {e}")
        return False

def _host_is_arm64() -> bool:
    return os.uname().machine.lower() in ("aarch64", "arm64")


def build_and_push_arm64_image(local_tag: str, ecr_uri: str) -> bool:
    """Build an ARM64 image and push it to ECR (native build on ARM64 hosts only)."""
    if not _host_is_arm64():
        print("Error: AgentCore requires linux/arm64 images.", flush=True)
        print(f"  Current host architecture: {os.uname().machine}", flush=True)
        print("  Build on an ARM64 EC2 instance (e.g. t4g, m7g) and retry.", flush=True)
        return False

    if not run_docker_command(
        ["docker", "build", "--platform", "linux/arm64", "-t", local_tag, "."],
        "Building Docker Image",
    ):
        return False
    if not run_docker_command(
        ["docker", "tag", local_tag, ecr_uri],
        "Tagging for ECR Repository",
    ):
        return False
    return run_docker_command(
        ["docker", "push", ecr_uri],
        "Pushing Image to ECR Repository",
    )


def run_docker_command(command, description):
    """Run Docker command with live output (plain BuildKit progress)."""
    print(f"===== {description} =====", flush=True)
    print(f"  $ {' '.join(command)}", flush=True)
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "BUILDKIT_PROGRESS": "plain"}
    try:
        result = subprocess.run(
            command,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            print(f"Error: {description} failed (exit {result.returncode})", flush=True)
            if command and command[0] == "docker" and "build" in command:
                print(
                    "  If the build log shows 'exec format error', use an ARM64 EC2 instance "
                    "(e.g. t4g, m7g).",
                    flush=True,
                )
                print(
                    "  If the build log shows 'no space left on device', run "
                    "'docker system prune -af' and retry.",
                    flush=True,
                )
            return False
        return True
    except Exception as e:
        print(f"Error: {description} failed: {e}", flush=True)
        return False

def push_to_ecr():
    """Build Docker image and push to ECR"""
    print(f"\n{'='*60}")
    print("Building Docker image and pushing to ECR")
    print(f"{'='*60}")
    
    try:
        # Use current time as tag
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        
        # Load config
        config = load_config()
        aws_account_id = config.get("accountId")
        aws_region = config.get("region")
        project_name = config.get("projectName")
        
        if not all([aws_account_id, aws_region, project_name]):
            print("Error: Missing required configuration in config.json")
            print("Required: accountId, region, projectName")
            return False
        
        # Get current folder name
        current_folder_name = os.path.basename(os.getcwd())
        print(f"CURRENT_FOLDER_NAME: {current_folder_name}")
        
        # Construct ECR repository name
        ecr_repository = f"{project_name}_{current_folder_name}"
        print(f"ECR_REPOSITORY: {ecr_repository}")
        
        # Construct image tag and ECR URI
        image_tag = timestamp
        ecr_uri = f"{aws_account_id}.dkr.ecr.{aws_region}.amazonaws.com/{ecr_repository}:{image_tag}"
        
        # Display configuration
        print("===== Checking AWS Configuration =====")
        print(f"AWS Account ID: {aws_account_id}")
        print(f"AWS Region: {aws_region}")
        print(f"ECR Repository: {ecr_repository}")
        print(f"ECR URI: {ecr_uri}")
        
        # Check AWS CLI
        if not check_aws_cli():
            return False
        
        # Check AWS credentials
        print("===== Checking AWS Credentials =====")
        if not check_aws_credentials():
            return False
        
        # Check/create ECR repository
        print("===== Checking ECR Repository =====", flush=True)
        ecr_client = boto3.client("ecr", region_name=aws_region)
        if not ensure_ecr_repository(ecr_client, ecr_repository, aws_region):
            return False

        if not check_docker_daemon():
            return False

        if not ensure_docker_disk_space():
            return False

        print("===== AWS ECR Login =====", flush=True)
        if not docker_login(aws_account_id, aws_region):
            return False
        
        # Build Docker image
        print("Build output streams below (this may take several minutes)...", flush=True)
        local_tag = f"{ecr_repository}:{image_tag}"
        if not build_and_push_arm64_image(local_tag, ecr_uri):
            return False
        
        # Complete
        print("===== Complete =====")
        print("Image has been successfully built and pushed to ECR.")
        print(f"Image URI: {ecr_uri}")

        remove_local_docker_images([local_tag, ecr_uri])
        
        # Store image tag in config for later use
        update_config('latest_image_tag', image_tag)
        update_config('ecr_repository', ecr_repository)
        
        return True
        
    except Exception as e:
        print(f"Error building and pushing Docker image: {e}")
        return False

# ============================================================================
# Agent Runtime Creation/Update Functions
# ============================================================================

def agent_runtime_name(runtime_type: str) -> str:
    """Return Bedrock AgentCore runtime name (e.g. runtime_langgraph)."""
    return f"runtime_{runtime_type.replace('-', '_')}"


def get_latest_image_tag(config):
    """Get the latest image tag from ECR."""
    try:
        aws_region = config['region']
        project_name = config.get('projectName')
        current_folder_name = os.path.basename(os.getcwd())
        repository_name = f"{project_name}_{current_folder_name}"
        
        ecr_client = boto3.client('ecr', region_name=aws_region)
        response = ecr_client.describe_images(repositoryName=repository_name)
        images = response['imageDetails']
        
        if not images:
            print(f"Error: No images found in repository {repository_name}")
            return None
        
        # Get latest image
        images_sorted = sorted(images, key=lambda x: x['imagePushedAt'], reverse=True)
        latest_image = images_sorted[0]
        
        if 'imageTags' not in latest_image or not latest_image['imageTags']:
            print(f"Error: Latest image has no tags")
            return None
        
        image_tag = latest_image['imageTags'][0]
        print(f"Latest image tag: {image_tag}")
        return image_tag
        
    except Exception as e:
        print(f"Error getting latest image tag: {e}")
        return None

def update_agentcore_json(agent_runtime_arn, image_tag=None):
    """Update config.json with agent runtime ARN and sync application config."""
    try:
        update_config('agent_runtime_arn', agent_runtime_arn)
        if image_tag:
            update_config('latest_image_tag', image_tag)
        print(f"✓ config.json updated with agent_runtime_arn: {agent_runtime_arn}")

        app_config_path = os.path.join(_repo_root(), "application", "config.json")
        if os.path.isfile(app_config_path):
            with open(app_config_path, "r", encoding="utf-8") as f:
                app_config = json.load(f)
            app_config["agent_runtime_arn"] = agent_runtime_arn
            if image_tag:
                app_config["agent_latest_image_tag"] = image_tag
            with open(app_config_path, "w", encoding="utf-8") as f:
                json.dump(app_config, f, ensure_ascii=False, indent=2)
            print(f"✓ application/config.json synced (agent_latest_image_tag={image_tag})")
        return True
    except Exception as e:
        print(f"Error updating config.json: {e}")
        return False

SESSION_STORAGE_MOUNT_PATH = "/mnt/workspace"


def _uses_s3_files_session_storage(config: dict) -> bool:
    return bool(config.get("s3_files_access_point_arn"))


def session_storage_filesystem_configurations(config: dict):
    """Filesystem mount for LangGraph checkpoint persistence."""
    access_point_arn = config.get("s3_files_access_point_arn")
    if access_point_arn:
        return [
            {
                "s3FilesAccessPoint": {
                    "accessPointArn": access_point_arn,
                    "mountPath": SESSION_STORAGE_MOUNT_PATH,
                }
            }
        ]

    return [
        {
            "sessionStorage": {
                "mountPath": SESSION_STORAGE_MOUNT_PATH,
            }
        }
    ]


def agent_runtime_network_configuration(config: dict):
    """Use VPC mode when S3 Files access point is configured."""
    if not _uses_s3_files_session_storage(config):
        return {"networkMode": "PUBLIC"}

    subnets = config.get("agent_runtime_vpc_subnets") or []
    security_groups = config.get("agent_runtime_security_groups") or []
    if not subnets or not security_groups:
        print(
            "Error: s3_files_access_point_arn is set but agent_runtime_vpc_subnets "
            "or agent_runtime_security_groups is missing."
        )
        return None

    return {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": subnets,
            "securityGroups": security_groups,
        },
    }


def verify_session_storage_config(client, agent_runtime_id: str, config: dict) -> bool:
    """Confirm session storage is configured on the agent runtime."""
    response = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)
    filesystem_configs = response.get("filesystemConfigurations") or []
    for cfg in filesystem_configs:
        if _uses_s3_files_session_storage(config):
            s3_files = cfg.get("s3FilesAccessPoint") or {}
            mount_path = s3_files.get("mountPath")
            if mount_path == SESSION_STORAGE_MOUNT_PATH:
                print(
                    "✓ s3FilesAccessPoint verified: "
                    f"mountPath={mount_path}, arn={s3_files.get('accessPointArn')}"
                )
                return True
        else:
            session_storage = cfg.get("sessionStorage") or {}
            mount_path = session_storage.get("mountPath")
            if mount_path == SESSION_STORAGE_MOUNT_PATH:
                print(f"✓ sessionStorage verified: mountPath={mount_path}")
                return True

    storage_type = "s3FilesAccessPoint" if _uses_s3_files_session_storage(config) else "sessionStorage"
    print(
        f"⚠ {storage_type} not found in filesystemConfigurations. "
        f"Cold start may not restore {SESSION_STORAGE_MOUNT_PATH} checkpoint data."
    )
    print(f"  filesystemConfigurations: {filesystem_configs or None}")
    return False


def create_agent_runtime_func(config, repository_name, image_tag):
    """Create a new Agent Runtime."""
    aws_region = config['region']
    account_id = config['accountId']
    agent_runtime_role = config.get('agent_runtime_role')
    
    if not agent_runtime_role:
        print("Error: agent_runtime_role not found in config.json")
        return None
    
    runtime_type = repository_name.rsplit("_", 1)[-1]
    runtime_name = agent_runtime_name(runtime_type)
    print(f"Creating agent runtime: {runtime_name}")
    
    try:
        client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
        network_configuration = agent_runtime_network_configuration(config)
        if network_configuration is None:
            return None
        
        response = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            agentRuntimeArtifact={
                'containerConfiguration': {
                    'containerUri': f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/{repository_name}:{image_tag}"
                }
            },
            filesystemConfigurations=session_storage_filesystem_configurations(config),
            networkConfiguration=network_configuration,
            roleArn=agent_runtime_role
        )
        
        print(f"✓ Agent runtime created: {response['agentRuntimeArn']}")
        return response['agentRuntimeArn']
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConflictException':
            print(f"Agent runtime {runtime_name} already exists")
            return None
        else:
            print(f"Error creating agent runtime: {e}")
            return None
    except Exception as e:
        print(f"Error creating agent runtime: {e}")
        return None

def update_agent_runtime_func(config, repository_name, agent_runtime_id, image_tag):
    """Update an existing Agent Runtime."""
    aws_region = config['region']
    account_id = config['accountId']
    agent_runtime_role = config.get('agent_runtime_role')
    
    if not agent_runtime_role:
        print("Error: agent_runtime_role not found in config.json")
        return None
    
    print(f"Updating agent runtime: {repository_name}")
    
    try:
        client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
        network_configuration = agent_runtime_network_configuration(config)
        if network_configuration is None:
            return None
        
        response = client.update_agent_runtime(
            agentRuntimeId=agent_runtime_id,
            description="Update agent runtime",
            agentRuntimeArtifact={
                'containerConfiguration': {
                    'containerUri': f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/{repository_name}:{image_tag}"
                }
            },
            filesystemConfigurations=session_storage_filesystem_configurations(config),
            roleArn=agent_runtime_role,
            networkConfiguration=network_configuration,
            protocolConfiguration={"serverProtocol": "HTTP"}
        )
        
        print(f"✓ Agent runtime updated: {response['agentRuntimeArn']}")
        return response['agentRuntimeArn']
        
    except Exception as e:
        print(f"Error updating agent runtime: {e}")
        return None

def create_agent_runtime():
    """Create/update AgentCore runtime"""
    print(f"\n{'='*60}")
    print("Creating/updating AgentCore runtime")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        aws_region = config['region']
        project_name = config.get('projectName')
        
        # Get current folder name
        current_folder_name = os.path.basename(os.getcwd())
        repository_name = f"{project_name}_{current_folder_name}"
        runtime_name = agent_runtime_name(current_folder_name)
        
        print(f"Repository name: {repository_name}")
        print(f"Runtime name: {runtime_name}")
        
        # Get latest image tag
        image_tag = get_latest_image_tag(config)
        if not image_tag:
            print("Error: Could not get latest image tag")
            return False
        
        print(f"Using image tag: {image_tag}")
        if _uses_s3_files_session_storage(config):
            print("Session storage: S3 Files access point at /mnt/workspace (VPC mode)")
        else:
            print("Session storage: managed sessionStorage at /mnt/workspace (PUBLIC mode)")
        
        # Check if agent runtime already exists
        client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
        response = client.list_agent_runtimes()
        agent_runtimes = response.get('agentRuntimes', [])
        
        is_exist = False
        agent_runtime_id = None
        
        for agent_runtime in agent_runtimes:
            if agent_runtime['agentRuntimeName'] == runtime_name:
                print(f"Agent runtime {runtime_name} already exists")
                is_exist = True
                agent_runtime_id = agent_runtime['agentRuntimeId']
                break
        
        # Create or update agent runtime
        if is_exist:
            print(f"Updating agent runtime: {runtime_name}")
            agent_runtime_arn = update_agent_runtime_func(config, repository_name, agent_runtime_id, image_tag)
        else:
            print(f"Creating agent runtime: {runtime_name}")
            agent_runtime_arn = create_agent_runtime_func(config, repository_name, image_tag)
        
        if not agent_runtime_arn:
            print("Error: Failed to create/update agent runtime")
            return False

        resolved_runtime_id = agent_runtime_id
        if not resolved_runtime_id and agent_runtime_arn:
            resolved_runtime_id = agent_runtime_arn.rsplit("/", 1)[-1]

        if resolved_runtime_id and not verify_session_storage_config(
            client, resolved_runtime_id, config
        ):
            print(
                "Error: session storage filesystem configuration missing on agent runtime. "
                "Re-run this installer after confirming s3_files_access_point_arn in config.json."
            )
            return False

        # Update config.json
        update_agentcore_json(agent_runtime_arn, image_tag)
        
        print("\n✓ Agent runtime creation/update completed")
        return True
        
    except Exception as e:
        print(f"Error creating/updating agent runtime: {e}")
        return False


def setup_agentcore_observability():
    """Enable Transaction Search and trace delivery for AgentCore Observability."""
    print(f"\n{'='*60}")
    print("Configuring AgentCore Observability")
    print(f"{'='*60}")

    try:
        from observability import setup_agentcore_observability as configure_observability

        config = load_config()
        region = config.get("region")
        account_id = config.get("accountId")
        runtime_arn = config.get("agent_runtime_arn")

        if not region or not account_id:
            print("Warning: region or accountId missing in config.json; skipping observability setup")
            return True

        result = configure_observability(runtime_arn, region, account_id)
        warning = result.get("warning")
        if warning:
            print(f"Warning: {warning}")
        else:
            print("✓ AgentCore Observability configured")
        return True
    except Exception as e:
        print(f"Warning: AgentCore Observability setup failed: {e}")
        return True


def create_monitoring_dashboard():
    """Create or update CloudWatch dashboard for LangGraph AgentCore runtime."""
    print(f"\n{'='*60}")
    print("Creating CloudWatch monitoring dashboard")
    print(f"{'='*60}")

    try:
        from cloudwatch_metrics import create_bedrock_usage_dashboard, create_cloudwatch_dashboard

        config = load_config()
        project_name = config.get("projectName", "langgraph-runtime")
        region = config.get("region")
        agent_runtime_arn = config.get("agent_runtime_arn")

        if not region:
            print("Error: region not found in config.json")
            return False

        bedrock_dashboard = create_bedrock_usage_dashboard(region)
        if bedrock_dashboard:
            update_config("bedrock_usage_dashboard_name", bedrock_dashboard)

        dashboard = create_cloudwatch_dashboard(project_name, agent_runtime_arn, region)
        if dashboard:
            update_config("cloudwatch_dashboard_name", dashboard)
            return True

        if bedrock_dashboard:
            return True

        print("Warning: CloudWatch dashboard was not created")
        return True

    except Exception as e:
        print(f"Error creating CloudWatch dashboard: {e}")
        return False

# ============================================================================
# Main Function
# ============================================================================

def main():
    """Main function: Execute the entire installation process."""
    print("\n" + "="*60)
    print("AgentCore Runtime Installation Script")
    print("="*60)
    
    # Check config.json
    config = load_config()
    
    print(f"Configuration file loaded successfully")
    print(f"  - Project Name: {config.get('projectName')}")
    print(f"  - Region: {config.get('region')}")
    print(f"  - Account ID: {config.get('accountId')}")
    
    # Execute each step
    steps = [
        ("Updating Knowledge Base configuration", update_knowledge_base_config),
        ("Creating IAM policies and roles", create_iam_policies),
        ("Building Docker image and pushing to ECR", push_to_ecr),
        ("Creating/updating AgentCore runtime", create_agent_runtime),
        ("Configuring AgentCore Observability", setup_agentcore_observability),
        ("Creating CloudWatch monitoring dashboard", create_monitoring_dashboard),
    ]
    
    for step_name, step_func in steps:
        if not step_func():
            print(f"\nInstallation failed: Error occurred in step '{step_name}'.")
            print("   Previous steps completed, but installation was aborted.")
            sys.exit(1)
    
    # Output final results
    print("\n" + "="*60)
    print("All installation steps completed successfully!")
    print("="*60)
    
    # Output final config.json information
    config = load_config()
    
    role_arn = config.get('agent_runtime_role')
    arn = config.get('agent_runtime_arn')
    knowledge_base_name = config.get('knowledge_base_name')
    knowledge_base_id = config.get('knowledge_base_id')
    
    if knowledge_base_name:
        print(f"\nKnowledge Base Name: {knowledge_base_name}")
    if knowledge_base_id:
        print(f"Knowledge Base ID: {knowledge_base_id}")
    if role_arn:
        print(f"Created AgentCore Runtime Role ARN: {role_arn}")
    if arn:
        print(f"Created AgentCore Runtime ARN: {arn}")

    dashboard_name = config.get("cloudwatch_dashboard_name")
    bedrock_dashboard_name = config.get("bedrock_usage_dashboard_name")
    region = config.get("region", "us-west-2")
    if bedrock_dashboard_name:
        print(f"Bedrock Usage Dashboard: {bedrock_dashboard_name}")
        print(
            f"  https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{bedrock_dashboard_name}"
        )
    if dashboard_name:
        print(f"CloudWatch Dashboard: {dashboard_name}")
        print(
            f"  https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{dashboard_name}"
        )
    
    if role_arn and arn:
        print("\nInstallation complete!")
    else:
        print("\nInstallation completed with warnings!")

if __name__ == "__main__":
    main()
