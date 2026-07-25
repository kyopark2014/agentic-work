# AWS Infrastructure Installer

boto3를 사용하여 AWS 인프라 리소스를 생성하는 Python 스크립트입니다.  
CDK 스택과 동등한 AWS 인프라를 프로그래밍 방식으로 배포합니다.

## 목차

1. [개요](#개요)
2. [설정값](#설정값)
3. [생성되는 리소스](#생성되는-리소스)
4. [주요 함수](#주요-함수)
5. [실행 방법](#실행-방법)
6. [배포 순서](#배포-순서)
7. [AgentCore Runtime installer (별도)](#agentcore-runtime-installer-별도)

---

## 개요

이 스크립트는 **agentic-work** 프로젝트의 Web UI(ECS)와 Bedrock Knowledge Base 등 공통 AWS 인프라를 자동으로 생성합니다.

- **Web UI**: ECS Fargate (`application/` — FastAPI + 프론트엔드) — 사용자 입력·MCP/Skill 선택·결과 표시
- **LangGraph Agent**: AgentCore Runtime (`runtime_agent/langgraph/installer.py`) — 추론·MCP·Skill 실행
- MCP 서버는 Runtime 컨테이너 **내부 stdio subprocess**로 기동됩니다. (`runtime_mcp/` 별도 MCP Runtime은 이 저장소에서 사용하지 않습니다.)
- **S3 Files**: AgentCore 세션 checkpoint·ECS `tasks.db` 영속화를 위해 S3 버킷을 NFS로 마운트
- **공유 Secrets**: Tavily·Notion API Key를 계정/리전 공용 Secret(`tavilyapikey`, `notionapikey`)으로 관리

### 주요 특징
- **완전 자동화**: 단일 스크립트로 ECS·RAG·네트워킹 인프라 배포
- **멱등성**: 이미 존재하는 리소스는 재사용
- **에러 핸들링**: 각 단계별 예외 처리, 실패 시에도 `application/config.json`에 부분 정보 저장
- **로깅**: 상세한 배포 진행 상황 출력
- **Secrets Manager**: 프로젝트 비의존 공유 Secret (`tavilyapikey`, `notionapikey`)
- **S3 Vectors 기반 RAG**: Bedrock Knowledge Base가 OpenSearch Serverless 대신 S3 Vectors를 벡터 스토어로 사용
- **S3 Files 세션 스토리지**: AgentCore `/mnt/workspace` + ECS `/mnt/app-data` 영속 마운트
- **ECS Fargate 배포**: multi-stage Dockerfile 이미지를 ECR에 push한 뒤 ECS Fargate(ARM64) 서비스로 실행
- **AgentCore 연동**: CloudFront URL·S3 Files 반영 후 LangGraph Runtime installer를 자동 호출
- **SSE 장시간 스트림**: ALB idle timeout·CloudFront origin read timeout을 120초로 설정
- **CloudFront→ALB 오리진 보호**: Secrets Manager 랜덤 헤더 주입 + ALB default 403 (헤더 일치 시에만 forward)
- **AgentCore Web Search Gateway**: `us-east-1` managed web-search connector

### 사전 요구사항
- **ARM64 빌드 호스트**: ECS/AgentCore 이미지는 `linux/arm64` 네이티브 빌드만 지원 (예: t4g, m7g EC2). x86 호스트에서는 QEMU 크로스 빌드 없이 즉시 실패합니다.
- **Docker CLI + buildx**: ARM64 호스트에서 컨테이너 이미지 빌드 및 ECR push (`docker buildx build --push`)
- **디스크 여유**: Docker 빌드 전 최소 약 2GB 여유 공간 확인 (`DOCKER_MIN_FREE_MB`)
- **AWS CLI**: ECR 로그인 (`aws ecr get-login-password`)
- **boto3** 및 스크립트 실행에 필요한 AWS 자격 증명 (S3 Files API용 `s3files`, Secrets Manager 포함)
- **IAM 권한**: S3, IAM, VPC, ECS, ECR, CloudFront, Bedrock Agent, S3 Vectors, AgentCore Control, **S3 Files**, Secrets Manager 등
- **Tavily / Notion API Key** (선택): 최초 Secret 생성 시 프롬프트로 입력 (Enter로 건너뛰기 가능)

---

## 설정값

```python
# 기본 설정 (installer.py 상단)
project_name = "agentic-work"        # 프로젝트 이름 (최소 3자)
region = "us-west-2"                 # AWS 리전 (ECS·VPC·KB 등)
AGENTCORE_GATEWAY_REGION = "us-east-1"  # AgentCore Web Search Gateway 전용 리전
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
AGENTCORE_WEBSEARCH_TARGET_NAME = "websearch"
git_name = "agentic-work"            # Git 저장소 이름 (레거시 EC2 SSM 배포용)

# SSE / ALB 타임아웃 (장시간 tool run 대비)
SSE_ORIGIN_READ_TIMEOUT_SECONDS = 120
ALB_IDLE_TIMEOUT_SECONDS = 120

# 자동 생성되는 변수
account_id = sts_client.get_caller_identity()["Account"]
bucket_name = f"storage-for-{project_name}-{account_id}-{region}"
vector_bucket_name = f"{project_name}-{account_id}"
vector_index_name = project_name

# 벡터 인덱스 설정
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"

# S3 Files (AgentCore session storage)
S3_FILES_SESSION_PREFIX = "agentcore-sessions/"

# AgentCore Runtime 이름: project_name의 '-' → '_' (예: agentic_work)
# agent_runtime_name(project_name)

# CloudFront→ALB 오리진 검증 헤더
custom_header_name = "X-Custom-Header"
# 값은 소스에 두지 않음. Secrets Manager:
#   {project_name}/cloudfront-alb-origin-header
# get_or_create_alb_origin_header()가 최초 배포 시 랜덤 생성·이후 재사용
ALB_ORIGIN_HEADER_SECRET_NAME = f"{project_name}/cloudfront-alb-origin-header"
```

---

## 생성되는 리소스

### 0. Secrets Manager (공유)

프로젝트 이름에 묶이지 않는 **계정/리전 공용** Secret입니다. 다른 프로젝트에서도 재사용합니다.

| Secret | 이름 | 내용 |
|--------|------|------|
| Tavily API Key | `tavilyapikey` | `{"tavily_api_key": "..."}` |
| Notion API Key | `notionapikey` | `{"notion_api_key": "..."}` |

- **최초 생성 시**: 설치 프롬프트에서 API Key 입력 (Enter로 건너뛰면 빈 값 저장)
- **이미 존재할 때**: 기존 Secret을 재사용하며 **값을 갱신하지 않음**
- **Runtime에서 사용**: AgentCore Runtime이 Secrets Manager에서 로드 (프로젝트별 `tavilyapikey-{project}` 패턴이 아님)

### 1. S3 버킷
- **이름**: `storage-for-{project_name}-{account_id}-{region}`
- **설정**:
  - CORS 활성화 (GET, POST, PUT)
  - 퍼블릭 액세스 차단
  - 버전 관리 **Enabled** (S3 Files file system 생성 필수; 신규 bucket은 `create_s3_bucket`에서, 기존 bucket은 `create_s3_files_session_storage` 시 자동 활성화)
  - `docs/` 폴더 자동 생성
  - S3 Files 세션 prefix: `agentcore-sessions/`

### 2. IAM 역할 (루트 installer)

| 역할 | 설명 |
|------|------|
| `role-knowledge-base-for-{project_name}-{region}` | Bedrock Knowledge Base용 역할 (S3 Vectors 접근 포함) |
| `role-agent-for-{project_name}-{region}` | Bedrock Agent용 역할 |
| `role-ecs-task-for-{project_name}-{region}` | ECS 태스크용 역할 (Bedrock, S3, AgentCore invoke, Secrets Manager, S3 Files mount 등) |
| `role-ecs-execution-for-{project_name}-{region}` | ECS 태스크 실행 역할 (ECR pull, CloudWatch Logs) |
| `role-agentcore-gateway-websearch-for-{project_name}` | AgentCore Web Search gateway용 역할 (`us-east-1`) |
| `role-s3files-sync-for-{project_name}` | S3 Files ↔ S3 bucket 동기화 역할 (`elasticfilesystem.amazonaws.com` trust) |

> `create_lambda_role()`, `create_agentcore_memory_role()` 함수는 코드에 남아 있으나, 현재 `main()` 배포 흐름에서는 호출되지 않습니다.

**AgentCore Runtime IAM** (`AmazonBedrockAgentCoreRuntimeRoleFor{project_name}`)은 `runtime_agent/langgraph/installer.py`가 별도로 생성·관리합니다. S3 Files 사용 시 `s3files:ClientMount` 등 권한이 조건부로 추가됩니다. ECS Task Role에는 `ensure_ecs_task_s3files_policy()`로 mount 권한이 추가됩니다.

### 3. S3 Vectors (벡터 스토어)
- **벡터 버킷**: `{project_name}-{account_id}`
- **인덱스**: `{project_name}` (1024차원, cosine, float32)
- **메타데이터**: Bedrock 필수 키(`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`)를 non-filterable로 설정

### 4. VPC 네트워킹

```
VPC (10.20.0.0/16)
├── Public Subnets (2개 AZ)
│   ├── Internet Gateway 연결
│   └── NAT Gateway 호스팅
├── Private Subnets (2개 AZ)
│   └── NAT Gateway + VPC Endpoints (아웃바운드)
├── Security Groups
│   ├── ALB SG (포트 80)
│   ├── ECS SG (포트 8501, 443)
│   ├── agent-runtime-sg-for-{project_name} (AgentCore microVM)
│   └── s3files-mount-sg-for-{project_name} (NFS 2049)
└── VPC Endpoints
    ├── Interface: bedrock-runtime, ecr.api, ecr.dkr, logs,
    │              secretsmanager, bedrock-agentcore,
    │              bedrock-agentcore-control
    └── Gateway: S3 (ECR 레이어 pull용)
```

### 4.5. S3 Files (AgentCore · ECS Session Storage)

VPC 생성 직후 `create_s3_files_session_storage()`가 아래를 **멱등**으로 프로비저닝합니다.

| 리소스 | 설명 |
|--------|------|
| Sync IAM role | `role-s3files-sync-for-{project_name}` — S3 bucket ↔ NFS 동기화 |
| File system | bucket `storage-for-...`, prefix `agentcore-sessions/` |
| Mount targets | private subnet마다 1개 (Runtime·ECS와 AZ 정렬) |
| Access point | 마운트 진입점 (`posix uid/gid: 0/0`) |
| Client SGs | `agent-runtime-sg` + ECS SG → NFS 2049 |

- **AgentCore**: `/mnt/workspace`에 마운트 → LangGraph checkpoint (`langgraph_checkpoints.sqlite`)
- **ECS**: `/mnt/app-data`에 마운트 → `TASK_DB_MOUNT` / `TASK_DB_PROJECT`로 `tasks.db` 영속화
- Runtime SG를 Bedrock/AgentCore/Secrets VPC endpoint에 연결 (`_ensure_agent_runtime_vpc_endpoint_access`)

`apply_s3_files_config()`가 `application/config.json`에 `s3_files_*`, `agent_runtime_vpc_*` 키를 기록합니다.  
Runtime은 access point ARN이 있으면 **`s3FilesAccessPoint` + VPC 모드**, 없으면 managed **`sessionStorage` + PUBLIC** 으로 생성합니다.

### 5. Application Load Balancer
- **타입**: Internet-facing Application Load Balancer
- **리스너**: HTTP 포트 80
- **타겟 그룹**: ECS Fargate 태스크 (IP 타겟, 포트 8501)
- **헬스체크**: `/api/health`
- **Idle timeout**: 120초 (`ALB_IDLE_TIMEOUT_SECONDS`) — 장시간 SSE 스트림 유지
- **Stickiness**: `lb_cookie` 86400초 (태스크별 SQLite working-copy 일관성)
- **Origin 보호**: listener default = **403 fixed-response**, `X-Custom-Header` 일치 시에만 ECS target group으로 forward (`ensure_alb_listener_origin_protection`)

### 6. CloudFront 배포
- **오리진**:
  - 기본: ALB (동적 컨텐츠) — Secrets Manager 오리진 헤더를 Custom Header로 주입
  - `/images/*`, `/docs/*`, `/artifacts/*`: S3 (정적 컨텐츠)
- **캐시 정책**: Managed-CachingDisabled
- **프로토콜**: HTTP → HTTPS 리다이렉트
- **Origin read timeout**: 120초 (`SSE_ORIGIN_READ_TIMEOUT_SECONDS`)
- **재사용**: 동일 `project_name`의 기존 CloudFront 배포가 있으면 재사용 (헤더·타임아웃·`/artifacts/*` behavior 갱신)

### 6.5. Secrets Manager (ALB origin header)
- **이름**: `{project_name}/cloudfront-alb-origin-header`
- **용도**: CloudFront → ALB 오리진 검증용 `X-Custom-Header` 값 (랜덤, 소스 하드코딩 없음)
- **생성**: `get_or_create_alb_origin_header()` / 삭제: `uninstaller.delete_alb_origin_header_secret()`

### 6.6. Secrets Manager (HMAC session signing key)
- **이름**: `{project_name}/session-signing-key`
- **용도**: Web UI `agent_user_id` 쿠키 HMAC-SHA256 서명 키
- **주입**: ECS env `SESSION_SIGNING_KEY` (`APP_CONFIG_JSON`에는 포함하지 않음)
- **생성**: `get_or_create_session_signing_key()` / 삭제: `uninstaller.delete_session_signing_key_secret()`

### 7. ECR (Elastic Container Registry)
- **리포지토리**: `ecr-for-{project_name}`
- **이미지 태그**: 배포 시각 기반 (`YYYYMMDDHHMMSS`) + ECR에서 `latest`로 promote
- **플랫폼**: `linux/arm64`
- **빌드 소스**: 프로젝트 루트 multi-stage `Dockerfile` (Node frontend + Python FastAPI)
- **빌드 방식**: `docker buildx build --platform linux/arm64 --provenance=false --sbom=false --push`

### 8. ECS Fargate
- **클러스터**: `cluster-for-{project_name}`
- **서비스**: `service-for-{project_name}`
- **태스크 정의**: `task-for-{project_name}`
- **런타임 플랫폼**: `ARM64` / `LINUX` (`runtimePlatform`)
- **컨테이너**: `app` (포트 8501, `uvicorn application.server:app`)
- **CPU / Memory**: 1024 / 2048
- **배포 위치**: Private Subnet (퍼블릭 IP 없음)
- **컨테이너 헬스체크**: `curl -f http://localhost:8501/api/health`
- **볼륨**: S3 Files → `/mnt/app-data` (설정 시)
- **배포 설정**: `minimumHealthyPercent=0`, `maximumPercent=100`, AZ rebalancing DISABLED
- **로그**: CloudWatch Logs `/ecs/app-for-{project_name}`

### 9. Bedrock Knowledge Base
- **스토리지**: S3 Vectors (`S3_VECTORS` 타입)
- **임베딩 모델**: Amazon Titan Embed Text v2 (1024차원, FLOAT32)
- **파싱**: 기본 파서 (default parser)
- **청킹**: Fixed Size (300 토큰, 20% 오버랩)
- **데이터 소스**: S3 `docs/` 프리픽스

> `create_opensearch_collection()` 함수는 이전 버전 호환을 위해 코드에 남아 있으나, 현재 배포 흐름에서는 사용하지 않습니다.

### 10. AgentCore 리소스

#### AgentCore Web Search Gateway
- **이름**: `gateway-websearch`
- **타겟 이름**: `websearch`
- **리전**: `us-east-1`
- **프로토콜**: MCP (`AWS_IAM` 인증)
- **타겟**: managed `web-search` connector
- **재사용**: 기존 gateway + websearch target이 있으면 재생성 없이 `application/config.json`에 반영

#### LangGraph Agent Runtime
VPC·S3 Files 프로비저닝 **후**, CloudFront로 `sharing_url`이 반영된 뒤 루트 installer가 `runtime_agent/langgraph/installer.py`를 자동 호출합니다 (`[11/10]`).

| 런타임 | 설치 스크립트 | Runtime 이름 |
|--------|--------------|--------------|
| LangGraph Agent | `runtime_agent/langgraph/installer.py` | `agent_runtime_name(project)` → `agentic_work` |

Runtime installer가 생성·갱신하는 주요 리소스:
- IAM: `AmazonBedrockAgentCoreRuntimePolicyFor{project_name}`, `AmazonBedrockAgentCoreRuntimeRoleFor{project_name}`
- AgentCore Runtime: `agentic_work` (하이픈 → 언더스코어)
- **Session storage (기본)**: S3 Files `s3FilesAccessPoint` @ `/mnt/workspace` + `networkMode: VPC`
- **Fallback**: managed `sessionStorage` + `PUBLIC`
- Checkpoint: `AsyncSqliteSaver` → `/mnt/workspace/langgraph_checkpoints.sqlite`
- CloudWatch Logs: `/aws/bedrock-agentcore/runtimes/{runtime_name}-...-DEFAULT`

> OpenAI GPT 5.4/5.5는 Bedrock Mantle Responses API를 사용합니다. Runtime IAM의 `BedrockMantleAccess`에 **모델이 호출하는 Mantle 리전**이 포함되어야 합니다.

---

## 주요 함수

### 인프라 생성 함수

#### `create_secrets()`
공유 Secrets Manager Secret 생성 (Tavily / Notion)

```python
def create_secrets() -> Dict[str, str]:
    """Create shared Secrets Manager secrets for Tavily and Notion API keys."""
    # tavilyapikey / notionapikey — 프로젝트 비의존, 계정·리전 공용
    # 최초 생성 시에만 API Key 입력 프롬프트
    return secret_arns  # {"tavily": "...", "notion": "..."}
```

#### `create_s3_bucket()`
S3 버킷 생성, CORS·퍼블릭 액세스 차단, **versioning Enabled**

#### `create_iam_role()` / `create_knowledge_base_role()` / `create_agent_role()` / `create_ecs_roles()` / `create_agentcore_websearch_gateway_role()`
각 서비스별 IAM 역할 및 인라인 정책 생성

#### `create_s3_vectors_store()` / `create_knowledge_base_with_s3_vectors()`
S3 Vectors 벡터 버킷·인덱스 생성 및 Bedrock Knowledge Base 연결

#### `create_vpc()` / `create_alb()` / `create_cloudfront_distribution()`
VPC·ALB·CloudFront 생성

- VPC: Bedrock Runtime + `ensure_private_subnet_vpc_endpoints()` (ECR, Logs, Secrets, AgentCore, S3 gateway)
- ALB: `ensure_alb_idle_timeout()` (120초), SG는 CloudFront prefix list만 허용
- CloudFront: ALB 오리진에 `X-Custom-Header` 주입, origin read timeout 120초, `/images/*`·`/docs/*`·`/artifacts/*`

#### `get_or_create_alb_origin_header()` / `ensure_alb_listener_origin_protection()`
Secrets Manager에 오리진 헤더 시크릿을 생성·재사용하고, ALB listener를 default 403 + 헤더 일치 시 forward로 맞춤

#### `create_s3_files_session_storage(vpc_info, s3_bucket_name, *, ecs_sg_id="", ecs_task_role_name="")`
AgentCore·ECS용 S3 Files 세션 스토리지 프로비저닝 (멱등).

```python
def create_s3_files_session_storage(
    vpc_info: Dict[str, str],
    s3_bucket_name: str,
    *,
    ecs_sg_id: str = "",
    ecs_task_role_name: str = "",
) -> Dict[str, object]:
    # sync role / file system / access point / mount targets
    # agent-runtime-sg + ECS SG + NFS SG
    # ensure_ecs_task_s3files_policy + VPC endpoint SG
    return {
        "file_system_id": "...",
        "file_system_arn": "...",
        "access_point_arn": "...",
        "subnets": [...],
        "security_groups": [...],
    }
```

#### `apply_s3_files_config(app_config, s3_files_info)`
S3 Files·VPC 키를 `application/config.json`에 병합

#### `create_ecr_repository()` / `build_and_push_docker_image()`
ECR 생성, ARM64 Docker buildx 빌드·push (타임스탬프 태그 → `latest` promote)

#### `deploy_ecs_service(..., s3_files_info=None)`
ECS Fargate 배포 (S3 Files 볼륨 `/mnt/app-data`, stickiness, `/api/health`)

#### `get_or_create_agentcore_websearch_gateway()`
AgentCore Web Search gateway 및 managed web-search 타겟 생성/조회

#### `sync_application_capability_lists()`
`runtime_agent/langgraph/mcp.list`, `skills.list` → `application/` 복사

#### `build_app_environment()` / `write_application_config()` / `_merge_runtime_agent_settings()`
`application/config.json` 생성·병합

#### `install_agent_runtime(runtime_type="langgraph")`
`runtime_agent/langgraph/installer.py` subprocess 실행

### 헬퍼 함수

| 함수 | 설명 |
|------|------|
| `agent_runtime_name()` | `project_name`의 `-` → `_` |
| `s3_vectors_bucket_arn()` / `s3_vectors_index_arn()` | S3 Vectors ARN |
| `ensure_private_subnet_vpc_endpoints()` | ECR/Logs/Secrets/AgentCore/S3 엔드포인트 |
| `ensure_alb_idle_timeout()` | ALB idle timeout 120초 |
| `get_or_create_alb_origin_header()` | Secrets Manager 오리진 헤더 생성·재사용 |
| `get_or_create_session_signing_key()` | Secrets Manager HMAC 세션 키 생성·재사용 → ECS `SESSION_SIGNING_KEY` |
| `ensure_alb_listener_origin_protection()` | ALB default 403 + 커스텀 헤더 forward |
| `_ensure_cloudfront_alb_origin_config()` / `_ensure_cloudfront_s3_path_behavior()` | CF 헤더·타임아웃·S3 path |
| `_get_or_create_s3files_*` / `ensure_ecs_task_s3files_policy` | S3 Files 프로비저닝 |
| `_ensure_agent_runtime_vpc_endpoint_access()` | Runtime SG → VPC endpoint |
| `_ensure_native_buildx_builder()` / `_ensure_docker_disk_space()` / `_promote_ecr_image_tag()` | Docker 빌드 |
| `create_alb_target_group_for_ecs()` | IP 타겟 그룹 + stickiness |
| `_wait_for_ecs_service_ready()` | ECS 안정화 대기 |
| `check_application_ready()` | CloudFront readiness |
| `build_config_from_deployment_state()` | 부분 배포 config 복구 |

### 레거시 EC2 함수 (main()에서 미사용)

| 함수 | 설명 |
|------|------|
| `create_opensearch_collection()` | OpenSearch Serverless (레거시) |
| `get_setup_script()` / `run_setup_script_via_ssm()` | EC2 SSM 설정 |
| `create_ec2_instance()` / `create_alb_target_group_and_listener()` | EC2 배포 |
| `verify_ec2_subnet_deployment()` | EC2 서브넷 검증 |

---

## 실행 방법

### 기본 실행 (전체 인프라 배포)

```bash
python installer.py
```

설치 시작 시 Tavily·Notion API Key 입력 프롬프트가 표시됩니다 (Secret이 이미 있으면 건너뜀).  
ARM64 EC2에서 Docker buildx로 Web UI 이미지를 빌드·push하고, LangGraph Agent Runtime을 설치한 뒤 ECS Fargate를 생성합니다.

로컬 테스트 (config 선기록 후):

```bash
uvicorn application.server:app --host 0.0.0.0 --port 8501
```

### Docker 빌드 생략

```bash
python installer.py --skip-docker-build
```

### Agent Runtime만 재설치

```bash
python installer.py --install-agent-runtime
python installer.py --install-agent-runtime langgraph
```

### 레거시 EC2

```bash
python installer.py --run-setup
python installer.py --run-setup i-1234567890abcdef0
python installer.py --verify-deployment
```

---

## 배포 순서

```
[0] Secrets Manager (공유)
       • tavilyapikey / notionapikey
       ↓
[1/10] S3 버킷 생성 (versioning Enabled)
       ↓
[2/10] IAM 역할 생성
       • Knowledge Base / Agent / ECS Task·Execution
       • AgentCore Web Search gateway 역할·gateway (us-east-1)
       ↓
[4/10] S3 Vectors 스토어 생성
       ↓
[4.5/10] Bedrock Knowledge Base 생성
       ↓
[5/10] VPC 네트워킹
       • VPC, 서브넷, IGW, NAT, ALB/ECS SG
       • VPC 엔드포인트 (Bedrock, ECR, Logs, Secrets, AgentCore, S3)
       ↓
[5.5/10] S3 Files 세션 스토리지
       • sync role, file system, mount targets, access point
       • agent-runtime-sg / ECS SG / s3files-mount-sg
       ↓
[5.6/10] ALB origin header (Secrets Manager 랜덤 생성·재사용)
       ↓
[6/10] Application Load Balancer (idle timeout 120초)
       ↓
[7/10] CloudFront (ALB 오리진 헤더 주입 + /images,/docs,/artifacts → S3)
       ↓
[8/10] 앱 설정·Runtime·컨테이너 이미지
       • mcp.list / skills.list 동기화
       • application/config.json (S3 Files + sharing_url)
       • [11/10] runtime_agent/langgraph/installer.py
       • ECS task AgentCore IAM refresh (`create_ecs_roles`)
       • ECR + buildx linux/arm64 빌드·push
       ↓
[9/10] ECS Fargate 배포
       • ALB listener: default 403, 헤더 일치 시 forward
       • stickiness + S3 Files /mnt/app-data
       ↓
[10/10] CloudFront readiness 확인
       ↓
완료 - application/config.json 업데이트 (finally)
```

---

## AgentCore Runtime installer (별도)

```bash
cd runtime_agent/langgraph
python installer.py
```

| 단계 | 함수 | 설명 |
|------|------|------|
| 1 | `update_knowledge_base_config()` | KB ID → `config.json` |
| 2 | `create_iam_policies()` | Runtime IAM (Bedrock, Mantle, ECR, Logs, S3 Files, Secrets 등) |
| 3 | `push_to_ecr()` | Runtime `linux/arm64` 빌드·push |
| 4 | `create_agent_runtime()` | `s3FilesAccessPoint` + VPC 또는 `sessionStorage` fallback |

Runtime 이름: `agent_runtime_name("agentic-work")` → `agentic_work`  
완료 후 `agent_runtime_arn` / `agent_runtime_role`이 `application/config.json`에 병합됩니다.

---

## 배포 완료 후

```
================================================================
Infrastructure Deployment Completed Successfully!
================================================================
Summary:
  S3 Bucket: storage-for-agentic-work-{account_id}-us-west-2
  VPC ID: vpc-xxxxxxxxx
  Public Subnets: subnet-xxx, subnet-yyy
  Private Subnets: subnet-aaa, subnet-bbb
  ALB DNS: http://alb-for-agentic-work-xxxxxx.us-west-2.elb.amazonaws.com/
  CloudFront Domain: https://xxxxxxxxx.cloudfront.net
  ECS Service: service-for-agentic-work (Fargate in private subnet)
  ECR Image: {account_id}.dkr.ecr.us-west-2.amazonaws.com/ecr-for-agentic-work:YYYYMMDDHHMMSS
  Build Number: YYYYMMDDHHMMSS
  S3 Vector Bucket: agentic-work-{account_id}
  S3 Vector Index ARN: arn:aws:s3vectors:...
  Knowledge Base ID: XXXXXXXXXX
  Knowledge Base Role: arn:aws:iam::...
  AgentCore Web Search Gateway: gateway-websearch (gateway-xxxxxxxx)
  AgentCore Web Search Gateway URL: https://...
  S3 Files Access Point: arn:aws:s3files:...
  Agent Runtime Subnets: subnet-aaa, subnet-bbb

Total deployment time: XX.XX minutes
================================================================
```

### application/config.json

| 필드 | 설명 |
|------|------|
| `projectName`, `accountId`, `region` | 프로젝트 기본 정보 |
| `knowledge_base_id`, `data_source_id`, `knowledge_base_role` | Bedrock KB |
| `vector_bucket_*`, `vector_index_*` | S3 Vectors |
| `s3_bucket`, `s3_arn` | 문서·세션 S3 |
| `s3_files_file_system_id`, `s3_files_access_point_arn` | S3 Files |
| `agent_runtime_vpc_subnets`, `agent_runtime_security_groups` | Runtime VPC |
| `sharing_url` | CloudFront URL |
| `agent_runtime_arn`, `agent_runtime_role` | LangGraph Runtime |
| `latest_image_tag`, `build_number` | ECS 이미지 태그 |
| `agentcore_websearch_gateway_*` | Web Search gateway |
| `collectionArn`, `opensearch_url` | 레거시 빈 값 |

ECS에는 `APP_CONFIG_JSON`이 주입되며, S3 Files 마운트 시 `TASK_DB_MOUNT=/mnt/app-data`, `TASK_DB_PROJECT=agentic-work`도 설정됩니다.  
Tavily/Notion 키는 config.json에 저장되지 않고 Secrets Manager(`tavilyapikey`, `notionapikey`)에서 읽습니다.

### Docker Container 구성

```text
# Stage 1: frontend build
FROM node:22-alpine AS frontend
WORKDIR /web
COPY application/web/package.json application/web/package-lock.json ./
RUN npm ci
COPY application/web/ .
RUN npm run build

# Stage 2: Python runtime
FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install fastapi python-multipart uvicorn[standard] boto3 \
    langchain_aws langchain-openai "openai>=2.41.0" \
    aws-bedrock-token-generator requests
COPY . .
COPY --from=frontend /web/dist /app/application/web/dist
RUN chmod +x /app/docker-entrypoint.sh
EXPOSE 8501
HEALTHCHECK CMD curl --fail http://localhost:8501/api/health
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "application.server:app", "--host", "0.0.0.0", "--port", "8501"]
```

### 주의사항
- Docker·ECS·AgentCore 모두 **ARM64** 전용 (t4g/m7g 등에서 실행)
- CloudFront 완전 활성화까지 15–20분 소요 가능
- `application/config.json`은 부분 배포 실패 시에도 `finally`에서 저장
- KB가 OpenSearch Serverless면 S3 Vectors 마이그레이션 시 삭제 후 재생성
- `TG-for-{project_name}`이 `instance` 타입이면 ECS 전 삭제 필요 (`ip` 타입)
- Private Subnet Fargate는 NAT + VPC Endpoint로 ECR pull
- S3 Files 사용 시 Runtime은 **VPC 모드** (mount AZ·SG 2049 정합 필요)
- S3 bucket **versioning Enabled** 필수
- Shared Secret이 이미 있으면 installer가 값을 갱신하지 않음 — Console/CLI로 수동 업데이트
- SSE tool run은 ALB/CloudFront 120초 타임아웃에 의존

---

## 에러 처리

| 상황 | 처리 방법 |
|------|----------|
| 리소스 / Secret 이미 존재 | 기존 리소스 재사용 (Secret 값 갱신 없음) |
| CloudFront / Web Search gateway 이미 존재 | 재사용 및 config 반영 |
| 서브넷 부족 / CIDR 충돌 | 자동 생성·대체 CIDR |
| KB 스토리지 불일치 | KB 삭제 후 S3 Vectors 재생성 |
| ECS 서비스 이미 존재 | 새 태스크 정의로 업데이트 |
| LangGraph Runtime installer 실패 | 경고 후 ECS 배포 계속 |
| 비-ARM64 / Docker 디스크 부족 | 빌드 단계 즉시 실패 |
| S3 Files versioning 미활성 | `_ensure_s3_bucket_versioning_enabled()` 자동 처리 |

### S3 Files file system 생성 오류

```
Your bucket must have versioning enabled to create a file system.
```

신규 bucket은 `create_s3_bucket()`에서, 기존 bucket은 `_ensure_s3_bucket_versioning_enabled()`에서 Enabled로 맞춥니다.

---

## 인프라 삭제

```bash
python uninstaller.py
```

Runtime만:

```bash
cd runtime_agent/langgraph
python uninstaller.py
```

삭제 순서(요약): CloudFront 비활성화 → AgentCore Runtime → ECS → ALB → EC2(레거시) → NAT → S3 Files → VPC → KB / S3 Vectors → Gateway / Memory / 공유 Secrets / ALB origin header secret / IAM / S3 → CloudFront 삭제 → config.json 정리  

> 공유 Secret(`tavilyapikey`, `notionapikey`)은 다른 프로젝트에서도 쓸 수 있으므로, uninstaller가 삭제하지 않을 수 있습니다. 필요 시 Console/CLI에서 수동 삭제하세요.
