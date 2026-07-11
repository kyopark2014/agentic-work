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

이 스크립트는 **langgraph-runtime** 프로젝트의 Streamlit UI(ECS)와 Bedrock Knowledge Base 등 공통 AWS 인프라를 자동으로 생성합니다. LangGraph Agent 추론은 별도 AgentCore Runtime(`runtime_agent/langgraph/installer.py`)에 배포되며, MCP 서버는 Runtime 컨테이너 **내부 stdio subprocess**로 기동됩니다. (`runtime_mcp/` 별도 MCP Runtime은 이 저장소에서 사용하지 않습니다.)

### 주요 특징
- **완전 자동화**: 단일 스크립트로 ECS·RAG·네트워킹 인프라 배포
- **멱등성**: 이미 존재하는 리소스는 재사용
- **에러 핸들링**: 각 단계별 예외 처리, 실패 시에도 `application/config.json`에 부분 정보 저장
- **로깅**: 상세한 배포 진행 상황 출력
- **S3 Vectors 기반 RAG**: Bedrock Knowledge Base가 OpenSearch Serverless 대신 S3 Vectors를 벡터 스토어로 사용
- **ECS Fargate 배포**: Dockerfile 기반 Streamlit 이미지를 ECR에 push한 뒤 ECS Fargate(ARM64) 서비스로 실행
- **AgentCore 연동**: CloudFront URL 반영 후 LangGraph Runtime installer를 자동 호출
- **S3 Files 세션 스토리지**: AgentCore Runtime용 `/mnt/workspace` 영속 마운트 (LangGraph SQLite checkpoint, Version 업데이트 후에도 유지)

### 사전 요구사항
- **ARM64 빌드 호스트**: ECS/AgentCore 이미지는 `linux/arm64` 네이티브 빌드만 지원 (예: t4g, m7g EC2). x86 호스트에서는 QEMU 크로스 빌드 없이 즉시 실패합니다.
- **Docker CLI**: ARM64 호스트에서 컨테이너 이미지 빌드 및 ECR push (디스크 여유 **2GB 이상** 권장)
- **AWS CLI**: ECR 로그인 (`aws ecr get-login-password`)
- **boto3** 및 스크립트 실행에 필요한 AWS 자격 증명
- **IAM 권한**: EC2/로컬에서 installer를 실행하는 주체는 S3, IAM, VPC, ECS, ECR, CloudFront, Bedrock Agent, S3 Vectors, AgentCore Control, **S3 Files** (`s3files`) 등 작업 권한이 필요합니다.

---

## 설정값

```python
# 기본 설정 (installer.py 상단)
project_name = "langgraph-runtime"   # 프로젝트 이름 (최소 3자)
region = "us-west-2"                 # AWS 리전 (ECS·VPC·KB 등)
AGENTCORE_GATEWAY_REGION = "us-east-1"  # AgentCore Web Search Gateway 전용 리전
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
git_name = "langgraph-runtime"       # Git 저장소 이름 (레거시 EC2 SSM 배포용)

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

# 커스텀 헤더 (CloudFront-ALB 통신용)
custom_header_name = "X-Custom-Header"
custom_header_value = f"{project_name}_12dab15e4s31"
```

---

## 생성되는 리소스

### 1. S3 버킷
- **이름**: `storage-for-{project_name}-{account_id}-{region}`
- **설정**:
  - CORS 활성화 (GET, POST, PUT)
  - 퍼블릭 액세스 차단
  - 버전 관리 **Enabled** (S3 Files file system 생성 필수; 기존 bucket은 `create_s3_files_session_storage` 시 자동 활성화)
  - `docs/` 폴더 자동 생성

### 2. IAM 역할 (루트 installer)

| 역할 | 설명 |
|------|------|
| `role-knowledge-base-for-{project_name}-{region}` | Bedrock Knowledge Base용 역할 (S3 Vectors 접근 포함) |
| `role-agent-for-{project_name}-{region}` | Bedrock Agent용 역할 |
| `role-ecs-task-for-{project_name}-{region}` | ECS 태스크용 역할 (Bedrock, S3, AgentCore invoke, Secrets Manager 등) |
| `role-ecs-execution-for-{project_name}-{region}` | ECS 태스크 실행 역할 (ECR pull, CloudWatch Logs) |
| `role-agentcore-gateway-websearch-for-{project_name}` | AgentCore Web Search gateway용 역할 (`us-east-1`) |
| `role-s3files-sync-for-{project_name}` | S3 Files ↔ S3 bucket 동기화 역할 (`elasticfilesystem.amazonaws.com` trust) |

> `create_lambda_role()`, `create_agentcore_memory_role()` 함수는 코드에 남아 있으나, 현재 `main()` 배포 흐름에서는 호출되지 않습니다.

**AgentCore Runtime IAM** (`AmazonBedrockAgentCoreRuntimeRoleFor{project_name}`)은 `runtime_agent/langgraph/installer.py`가 별도로 생성·관리합니다. S3 Files 사용 시 `s3files:ClientMount` 등 권한이 조건부로 추가됩니다.

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
│   └── NAT Gateway를 통한 아웃바운드 (ECR pull, Bedrock API 등)
├── Security Groups
│   ├── ALB SG (포트 80)
│   ├── ECS SG (포트 8501, 443)
│   ├── agent-runtime-sg-for-{project_name} (AgentCore microVM)
│   └── s3files-mount-sg-for-{project_name} (NFS 2049)
└── VPC Endpoints
    └── Bedrock Runtime 엔드포인트 (agent runtime SG 추가 연결)
```

### 4.5. S3 Files (AgentCore Session Storage)

VPC 생성 직후 `create_s3_files_session_storage()`가 아래를 **멱등**으로 프로비저닝합니다.

| 리소스 | 설명 |
|--------|------|
| Sync IAM role | `role-s3files-sync-for-{project_name}` — S3 bucket ↔ NFS 동기화 |
| File system | bucket `storage-for-...`, prefix `agentcore-sessions/` |
| Mount targets | private subnet마다 1개 (Runtime과 AZ 정렬) |
| Access point | `/mnt/workspace` 마운트용 (`posix uid/gid: 0/0`) |

`apply_s3_files_config()`가 `application/config.json`에 `s3_files_*`, `agent_runtime_vpc_*` 키를 기록합니다.  
Runtime(`runtime_agent/langgraph/installer.py`)은 access point ARN이 있으면 **`s3FilesAccessPoint` + VPC 모드**, 없으면 managed **`sessionStorage` + PUBLIC** 으로 생성합니다. LangGraph `AsyncSqliteSaver`는 `/mnt/workspace/langgraph_checkpoints.sqlite`에 checkpoint를 저장합니다.

### 5. Application Load Balancer
- **타입**: Internet-facing Application Load Balancer
- **리스너**: HTTP 포트 80
- **타겟 그룹**: ECS Fargate 태스크 (IP 타겟, 포트 8501)
- **헬스체크**: `/api/health`

### 6. CloudFront 배포
- **오리진**:
  - 기본: ALB (동적 컨텐츠)
  - `/images/*`, `/docs/*`: S3 (정적 컨텐츠)
- **캐시 정책**: Managed-CachingDisabled
- **프로토콜**: HTTP → HTTPS 리다이렉트
- **재사용**: 동일 `project_name`의 기존 CloudFront 배포가 있으면 재사용

### 7. ECR (Elastic Container Registry)
- **리포지토리**: `ecr-for-{project_name}`
- **이미지 태그**: 배포 시각 기반 (`YYYYMMDDHHMMSS`) + `latest`
- **플랫폼**: `linux/arm64` (AgentCore runtime과 동일; ARM64 EC2에서 네이티브 빌드)
- **빌드 소스**: 프로젝트 루트의 `Dockerfile` (Streamlit UI 전용)

### 8. ECS Fargate
- **클러스터**: `cluster-for-{project_name}`
- **서비스**: `service-for-{project_name}`
- **태스크 정의**: `task-for-{project_name}`
- **런타임 플랫폼**: `ARM64` / `LINUX` (`runtimePlatform`)
- **컨테이너**: `app` (포트 8501)
- **CPU / Memory**: 1024 / 2048
- **배포 위치**: Private Subnet (퍼블릭 IP 없음)
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
- **리전**: `us-east-1` (AgentCore Gateway 전용)
- **프로토콜**: MCP (`AWS_IAM` 인증)
- **타겟**: managed `web-search` connector
- **용도**: Runtime의 `websearch` MCP (AgentCore Gateway 경유)
- **재사용**: 기존 gateway + websearch target이 있으면 재생성 없이 `application/config.json`에 반영

#### LangGraph Agent Runtime
VPC·S3 Files 프로비저닝 **후**, CloudFront 배포로 `sharing_url`이 반영된 뒤 루트 installer가 아래 스크립트를 **자동 호출**합니다.

| 런타임 | 설치 스크립트 | ECR 리포지토리 |
|--------|--------------|----------------|
| LangGraph Agent | `runtime_agent/langgraph/installer.py` | `{project_name}_langgraph` |

Runtime installer가 생성·갱신하는 주요 리소스:
- IAM 정책/역할: `AmazonBedrockAgentCoreRuntimePolicyFor{project_name}`, `AmazonBedrockAgentCoreRuntimeRoleFor{project_name}`
- AgentCore Runtime: `{project_name}_langgraph`
- **Session storage (기본)**: S3 Files `s3FilesAccessPoint` @ `/mnt/workspace` + `networkMode: VPC`
- **Fallback**: managed `sessionStorage` + `PUBLIC` (`s3_files_access_point_arn` 없을 때)
- Checkpoint: `chat.py` → `AsyncSqliteSaver` → `/mnt/workspace/langgraph_checkpoints.sqlite`
- CloudWatch Logs: `/aws/bedrock-agentcore/runtimes/{project_name}_langgraph-...-DEFAULT`

> OpenAI GPT 5.4/5.5는 Bedrock Mantle Responses API를 사용합니다. Runtime IAM 정책의 `BedrockMantleAccess`에 **모델이 호출하는 Mantle 리전**(예: GPT 5.5 → `us-east-2`)이 포함되어야 합니다. 기본 정책은 Runtime 배포 리전(`config.json`의 `region`)만 허용할 수 있습니다.

---

## 주요 함수

### 인프라 생성 함수

#### `create_s3_bucket()`
S3 버킷 생성, CORS·퍼블릭 액세스 차단, **versioning Enabled** (S3 Files 요구사항)

#### `create_iam_role()` / `create_knowledge_base_role()` / `create_agent_role()` / `create_ecs_roles()` / `create_agentcore_websearch_gateway_role()`
각 서비스별 IAM 역할 및 인라인 정책 생성

`create_ecs_roles()`는 아래 두 역할을 반환합니다.

```python
{
    "task_role_arn": "...",
    "execution_role_arn": "...",
}
```

#### `create_s3_vectors_store()` / `create_knowledge_base_with_s3_vectors()`
S3 Vectors 벡터 버킷·인덱스 생성 및 Bedrock Knowledge Base 연결

#### `create_vpc()` / `create_alb()` / `create_cloudfront_distribution()`
VPC·ALB·CloudFront 생성 (기존 CloudFront 재사용 로직 포함)

#### `create_s3_files_session_storage(vpc_info, s3_bucket_name)`
AgentCore Runtime용 S3 Files 세션 스토리지 프로비저닝 (멱등).

```python
def create_s3_files_session_storage(
    vpc_info: Dict[str, str],
    s3_bucket_name: str,
) -> Dict[str, object]:
    # 1. _get_or_create_s3files_sync_role()
    # 2. _ensure_s3_bucket_versioning_enabled()
    # 3. _get_or_create_s3files_file_system()  # prefix: agentcore-sessions/
    # 4. agent-runtime-sg + s3files-mount-sg (NFS 2049)
    # 5. _ensure_s3files_mount_targets() per private subnet
    # 6. _get_or_create_s3files_access_point()
    # 7. _add_security_group_to_vpc_endpoint()  # Bedrock endpoint
    return {
        "file_system_id": "...",
        "access_point_arn": "...",
        "subnets": [...],
        "security_groups": [...],
    }
```

#### `apply_s3_files_config(app_config, s3_files_info)`
S3 Files·VPC 키를 `application/config.json` 페이로드에 병합.

#### `create_ecr_repository()` / `build_and_push_docker_image()`
ECR 리포지토리 생성, ARM64 Docker 빌드·push

```python
def build_and_push_docker_image(repository_uri, image_tag=None) -> Tuple[str, str]:
    # _require_arm64_build_host() — ARM64 EC2(t4g, m7g) 필수
    # _ensure_docker_disk_space() — 최소 2048MB 여유 확인
    # image_tag 미지정 시 generate_image_build_tag() → YYYYMMDDHHMMSS
    # docker build --platform linux/arm64
    # docker push (태그 + latest)
    return image_uri, image_tag
```

#### `deploy_ecs_service()`
ECS Fargate 서비스 배포 (태스크 정의, ALB 연동 포함)

#### `get_or_create_agentcore_websearch_gateway()`
AgentCore Web Search gateway 및 managed web-search 타겟 생성/조회

#### `sync_application_capability_lists()`
`runtime_agent/langgraph/mcp.list`, `skills.list`를 `application/`으로 복사 (컨테이너 빌드 전)

#### `build_app_environment()` / `write_application_config()` / `_merge_runtime_agent_settings()`
컨테이너·로컬 개발용 `application/config.json` 생성. `apply_s3_files_config()`로 S3 Files 키 병합. Runtime installer가 기록한 `agent_runtime_arn` 등을 `_merge_runtime_agent_settings()`로 병합합니다.

#### `install_agent_runtime(runtime_type="langgraph")`
`runtime_agent/langgraph/installer.py`를 subprocess로 실행하여 AgentCore Runtime 배포 (S3 Files + VPC 모드 반영)

### 헬퍼 함수

| 함수 | 설명 |
|------|------|
| `s3_vectors_bucket_arn()` / `s3_vectors_index_arn()` | S3 Vectors ARN 생성 |
| `attach_inline_policy()` | IAM 역할에 인라인 정책 연결 |
| `ensure_data_source()` | Knowledge Base S3 데이터 소스 생성/조회 |
| `delete_knowledge_base()` | Knowledge Base 및 데이터 소스 삭제 |
| `create_security_group()` / `create_vpc_endpoint()` | 보안 그룹·VPC 엔드포인트 생성 |
| `_ensure_s3_bucket_versioning_enabled()` | S3 bucket versioning Enabled (S3 Files 필수) |
| `_get_or_create_s3files_sync_role()` | S3 Files sync IAM role |
| `_get_or_create_s3files_file_system()` | S3 Files file system (prefix `agentcore-sessions/`) |
| `_ensure_s3files_mount_targets()` | private subnet별 mount target |
| `_get_or_create_s3files_access_point()` | S3 Files access point |
| `_add_security_group_to_vpc_endpoint()` | Bedrock VPC endpoint에 runtime SG 추가 |
| `_wait_for_s3files_status()` | S3 Files 리소스 available 폴링 |
| `create_public_subnets()` / `create_private_subnets()` | 서브넷 생성 |
| `get_or_create_internet_gateway()` / `get_or_create_nat_gateway()` | IGW/NAT Gateway 조회/생성 |
| `classify_subnets()` | 서브넷을 퍼블릭/프라이빗으로 분류 |
| `wait_for_subnet_available()` / `wait_for_nat_gateway()` | 리소스 가용 상태 대기 |
| `create_ecs_log_group()` / `create_ecs_cluster()` | ECS 로그 그룹·클러스터 생성 |
| `create_alb_target_group_for_ecs()` / `create_alb_listener_with_target_group()` | Fargate용 타겟 그룹·리스너 |
| `_require_arm64_build_host()` | ARM64 EC2에서만 Docker 빌드 허용 |
| `_docker_build_platform()` / `_ecs_runtime_platform()` | `linux/arm64` / `ARM64` 플랫폼 상수 |
| `generate_image_build_tag()` / `resolve_ecr_image_uri()` | 이미지 태그 생성·ECR URI 조회 |
| `_ensure_docker_disk_space()` | Docker 빌드 전 디스크 여유 확인 (기본 2048MB) |
| `check_application_ready()` | CloudFront URL 애플리케이션 준비 상태 확인 |
| `build_config_from_deployment_state()` | 부분 배포 실패 시 config.json 복구용 payload 생성 |

### 레거시 EC2 함수 (main()에서 미사용)

| 함수 | 설명 |
|------|------|
| `get_setup_script()` | EC2 User Data / SSM 설정 스크립트 생성 |
| `run_setup_script_via_ssm()` | SSM Run Command로 설정 스크립트 실행 |
| `create_ec2_instance()` | EC2 인스턴스 생성 |
| `create_alb_target_group_and_listener()` | EC2 instance 타겟 그룹 등록 |
| `verify_ec2_subnet_deployment()` | EC2 서브넷 배포 검증 |

---

## 실행 방법

### 기본 실행 (전체 인프라 배포)

```bash
python installer.py
```

ARM64 EC2에서 Streamlit `linux/arm64` 이미지를 빌드·push하고, LangGraph Agent Runtime을 설치한 뒤 ECS Fargate(ARM64) 서비스를 생성합니다.

### Docker 빌드 생략 (기존 ECR 이미지 재사용)

```bash
python installer.py --skip-docker-build
```

ECR의 최신 이미지(`latest_image_tag` 또는 `latest`)를 재사용합니다. 인프라만 재배포하거나 태스크 정의만 갱신할 때 유용합니다.

### Agent Runtime만 재설치

```bash
python installer.py --install-agent-runtime
python installer.py --install-agent-runtime langgraph
```

루트 인프라 배포 없이 `runtime_agent/langgraph/installer.py`만 실행합니다. Runtime 이미지 재빌드·IAM·AgentCore Runtime 업데이트가 필요할 때 사용합니다.

### 레거시: 기존 EC2 인스턴스에 설정 스크립트 실행

```bash
python installer.py --run-setup
python installer.py --run-setup i-1234567890abcdef0
```

> 현재 기본 배포는 ECS Fargate입니다. `--run-setup`은 이전 EC2 배포 환경 호환용입니다.

### 레거시: EC2 서브넷 배포 검증

```bash
python installer.py --verify-deployment
```

---

## 배포 순서

스크립트는 다음 순서로 리소스를 생성합니다:

```
[1/10] S3 버킷 생성
       ↓
[2/10] IAM 역할 생성
       • Knowledge Base 역할
       • Agent 역할
       • ECS Task / Execution 역할
       • AgentCore Web Search gateway 역할 (없을 때만)
       • AgentCore Web Search gateway 생성/재사용 (us-east-1)
       ↓
[3/10] S3 Vectors 스토어 생성
       • 벡터 버킷 + 인덱스
       ↓
[4.5/10] Bedrock Knowledge Base 생성
       • S3 Vectors 연결
       • S3 데이터 소스 (docs/) 연결
       ↓
[5/10] VPC 네트워킹 리소스 생성
       • VPC, 서브넷, IGW, NAT Gateway
       • 보안 그룹 (ALB SG, ECS SG)
       • Bedrock Runtime VPC 엔드포인트
       ↓
[5.5/10] S3 Files 세션 스토리지 생성
       • sync role, file system, mount targets, access point
       • agent-runtime-sg / s3files-mount-sg (NFS 2049)
       • application/config.json에 S3 Files·VPC 키 기록
       ↓
[6/10] Application Load Balancer 생성
       ↓
[7/10] CloudFront 배포 생성 (또는 기존 배포 재사용)
       • OAI 생성, S3 버킷 정책 업데이트
       • ALB + S3 하이브리드 오리진
       ↓
[8/10] 앱 설정·Runtime·컨테이너 이미지
       • mcp.list / skills.list → application/ 동기화
       • application/config.json 생성 (S3 Files 키 + sharing_url 반영)
       • runtime_agent/langgraph/installer.py 실행
         (s3FilesAccessPoint + VPC 또는 sessionStorage fallback)
       • ECR 리포지토리 생성
       • Dockerfile 기반 linux/arm64 빌드 및 push (태그 + latest)
       ↓
[9/10] ECS Fargate 서비스 배포
       • CloudWatch Logs 그룹 생성
       • IP 타겟 그룹 + ALB 리스너 연결
       • 태스크 정의 등록 (runtimePlatform=ARM64, APP_CONFIG_JSON 포함)
       • Private Subnet에 Fargate ARM64 태스크 실행
       ↓
[10/10] 애플리케이션 준비 상태 확인 (CloudFront URL)
       ↓
완료 - application/config.json 업데이트 (finally 블록)
```

---

## AgentCore Runtime installer (별도)

LangGraph Agent는 루트 installer의 [8/10] 단계에서 자동 호출되거나, `--install-agent-runtime`으로 단독 실행할 수 있습니다.

```bash
cd runtime_agent/langgraph
python installer.py
```

| 단계 | 함수 | 설명 |
|------|------|------|
| 1 | `update_knowledge_base_config()` | 루트 `project_name`으로 KB ID 조회 → `config.json` 반영 |
| 2 | `create_iam_policies()` | Runtime IAM 정책·역할 (Bedrock AgentCore, Mantle, ECR, Logs 등) |
| 3 | `push_to_ecr()` | Runtime Dockerfile `linux/arm64` 빌드 → `{project_name}_langgraph` ECR push |
| 4 | `create_agent_runtime()` | AgentCore Runtime 생성/업데이트 (`s3FilesAccessPoint` + VPC 또는 `sessionStorage` fallback) |

Runtime installer 내부:
- `session_storage_filesystem_configurations(config)` — S3 Files / managed 분기
- `agent_runtime_network_configuration(config)` — VPC / PUBLIC 분기
- `load_config()` → `_merge_application_config()` — `application/config.json`의 S3 Files 키 동기화

완료 후 `runtime_agent/langgraph/config.json`에 `agent_runtime_arn`, `agent_runtime_role`이 기록되며, 루트 installer가 이를 `application/config.json`에 병합합니다.

---

## 배포 완료 후

배포가 완료되면 다음 정보가 출력됩니다:

```
================================================================
Infrastructure Deployment Completed Successfully!
================================================================
Summary:
  S3 Bucket: storage-for-langgraph-runtime-{account_id}-us-west-2
  VPC ID: vpc-xxxxxxxxx
  Public Subnets: subnet-xxx, subnet-yyy
  Private Subnets: subnet-aaa, subnet-bbb
  ALB DNS: http://alb-for-langgraph-runtime-xxxxxx.us-west-2.elb.amazonaws.com/
  CloudFront Domain: https://xxxxxxxxx.cloudfront.net
  ECS Service: service-for-langgraph-runtime (Fargate in private subnet)
  ECR Image: {account_id}.dkr.ecr.us-west-2.amazonaws.com/ecr-for-langgraph-runtime:YYYYMMDDHHMMSS
  Build Number: YYYYMMDDHHMMSS
  S3 Vector Bucket: langgraph-runtime-{account_id}
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

배포 성공/실패와 관계없이 `finally` 블록에서 `application/config.json`이 갱신됩니다. 주요 필드:

| 필드 | 설명 |
|------|------|
| `projectName`, `accountId`, `region` | 프로젝트 기본 정보 |
| `knowledge_base_id`, `data_source_id` | Bedrock Knowledge Base |
| `knowledge_base_role` | Knowledge Base IAM 역할 ARN |
| `vector_bucket_name`, `vector_bucket_arn` | S3 Vectors 버킷 |
| `vector_index_name`, `vector_index_arn` | S3 Vectors 인덱스 |
| `s3_bucket`, `s3_arn` | 문서 저장 S3 버킷 |
| `s3_files_file_system_id` | S3 Files file system ID (AgentCore session storage) |
| `s3_files_access_point_arn` | S3 Files access point ARN |
| `agent_runtime_vpc_subnets` | AgentCore Runtime VPC subnet ID 목록 |
| `agent_runtime_security_groups` | AgentCore Runtime security group ID 목록 |
| `sharing_url` | CloudFront URL |
| `agent_runtime_arn`, `agent_runtime_role` | LangGraph AgentCore Runtime (Runtime installer 결과) |
| `latest_image_tag`, `build_number` | ECS Streamlit 이미지 빌드 태그 |
| `agentcore_websearch_gateway_name` | AgentCore Web Search gateway 이름 |
| `agentcore_websearch_gateway_region` | AgentCore Web Search gateway 리전 (`us-east-1`) |
| `agentcore_websearch_gateway_id` | AgentCore Web Search gateway ID |
| `agentcore_websearch_gateway_url` | AgentCore Web Search gateway URL |
| `agentcore_websearch_gateway_role` | AgentCore Web Search gateway IAM 역할 ARN |
| `collectionArn`, `opensearch_url` | 레거시 호환용 빈 값 |

ECS 컨테이너에는 `APP_CONFIG_JSON` 환경변수로 동일한 설정이 주입되며, `docker-entrypoint.sh`가 시작 시 `application/config.json`으로 기록합니다.

### Docker Container 구성

ECS Streamlit 앱은 프로젝트 루트의 `Dockerfile`로 빌드됩니다. Agent 추론은 AgentCore Runtime(`runtime_agent/langgraph/Dockerfile`)에서 별도 `linux/arm64` 이미지로 배포됩니다.

빌드 시 `docker build --platform linux/arm64`를 사용하며, Dockerfile 자체에는 `--platform` 지정이 없습니다.

```text
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# ECS Web UI (FastAPI + React); agent runs on AgentCore
RUN pip install fastapi uvicorn boto3 langchain_aws langchain-openai "openai>=2.41.0" \
    aws-bedrock-token-generator requests

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/api/health

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "application.server:app", "--host", "0.0.0.0", "--port", "8501"]
```

`docker-entrypoint.sh`는 `APP_CONFIG_JSON` 환경변수가 있으면 `/app/application/config.json`을 생성한 뒤 FastAPI를 실행합니다.

### 주의사항
- Docker 이미지 빌드와 ECS Fargate·AgentCore Runtime 모두 **ARM64** 전용입니다. x86 Mac/EC2에서는 `installer.py`와 `runtime_agent/langgraph/installer.py` 모두 실패하므로, t4g/m7g 등 ARM64 EC2에서 실행하세요.
- CloudFront 배포는 완전히 활성화되기까지 15–20분이 소요될 수 있습니다.
- ECS Fargate 서비스가 안정화되고 ALB 헬스체크가 통과하기까지 수 분이 걸릴 수 있습니다.
- `application/config.json`은 부분 배포 실패 시에도 `finally`에서 저장됩니다.
- Knowledge Base가 기존 OpenSearch Serverless를 사용 중이면 S3 Vectors로 마이그레이션 시 자동 삭제 후 재생성됩니다.
- 기존 EC2 배포에서 생성된 `TG-for-{project_name}` 타겟 그룹이 `instance` 타입이면 ECS 배포 전 삭제가 필요합니다 (Fargate는 `ip` 타입 필요).
- Private Subnet의 Fargate 태스크는 NAT Gateway를 통해 ECR에서 이미지를 pull합니다.
- OpenAI Mantle 모델(GPT 5.4/5.5) 사용 시 Runtime IAM에 해당 Mantle 리전 권한이 있는지 CloudWatch runtime 로그로 확인하세요 (`bedrock-mantle:CreateInference` 401).
- S3 Files 사용 시 AgentCore Runtime은 **VPC 모드**이며, mount target AZ·SG(2049)가 맞아야 invoke가 성공합니다.
- S3 bucket **versioning Enabled**가 없으면 file system 생성이 실패합니다 (`ValidationException`).
- Managed `sessionStorage`만 사용할 경우 Runtime **Version 업데이트 시** `/mnt/workspace` checkpoint가 초기화됩니다 (S3 Files 권장).

---

## 에러 처리

스크립트는 다음과 같은 에러를 자동으로 처리합니다:

| 상황 | 처리 방법 |
|------|----------|
| 리소스 이미 존재 | 기존 리소스 재사용 |
| CloudFront / Web Search gateway 이미 존재 | 기존 리소스 재사용 및 config 반영 |
| 서브넷 부족 | 자동으로 서브넷 생성 |
| CIDR 충돌 | 대체 CIDR 블록 자동 선택 |
| 정책 이미 존재 | 기존 정책 업데이트 |
| KB 스토리지 불일치 | Knowledge Base 삭제 후 S3 Vectors로 재생성 |
| ECS 서비스 이미 존재 | 새 태스크 정의로 서비스 업데이트 (`forceNewDeployment`) |
| LangGraph Runtime installer 실패 | 경고 로그 후 ECS 배포는 계속 진행 |
| 비-ARM64 빌드 호스트 | Docker 빌드 단계에서 즉시 실패 (ARM64 EC2 사용 안내) |
| Docker 디스크 부족 | `_ensure_docker_disk_space()`에서 사전 검사 |
| S3 Files file system 생성 실패 | bucket versioning 미활성 → `_ensure_s3_bucket_versioning_enabled()` 자동 처리 |
| 타임아웃 | 재시도 로직 적용 (CloudFront readiness check 등) |

배포 실패 시 상세한 에러 메시지와 스택 트레이스가 출력되며, 가능한 배포 정보는 `application/config.json`에 저장됩니다.

### S3 Files file system 생성 오류

```
Your bucket must have versioning enabled to create a file system.
```

- `create_s3_bucket()`은 신규 bucket에 versioning **Enabled** 설정
- 기존 bucket은 `create_s3_files_session_storage()` 내 `_ensure_s3_bucket_versioning_enabled()`가 자동 활성화
- sync role(`role-s3files-sync-for-{project_name}`) 및 S3/EventBridge inline policy 확인

---

## 인프라 삭제

ECS/ECR·AgentCore Runtime 리소스를 포함한 전체 인프라 삭제:

```bash
python uninstaller.py
```

Runtime만 삭제:

```bash
cd runtime_agent/langgraph
python uninstaller.py
```

삭제 순서(요약): CloudFront 비활성화 → **AgentCore Runtime** (`runtime_agent/langgraph/uninstaller.py` 위임) → ECS → ALB → EC2(레거시) → NAT → **S3 Files** (access point / mount target / file system / sync role) → VPC → Knowledge Base / S3 Vectors → Gateway / IAM / S3 bucket → CloudFront 완전 삭제 → **`application/config.json`**, `runtime_agent/langgraph/config.json` 정리
