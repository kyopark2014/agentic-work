# LangGraph Agent의 AgentCore 배포 및 활용

여기에서는 Codex형 Web UI(FastAPI + React)를 Amazon ECS에 배포하고, Agent는 AgentCore Runtime을 활용해 배포합니다. 

## 주요 구현 

### 전체 Architecture

전체적인 Architecture는 아래와 같습니다. 여기서는 MCP/SKILL를 지원하는 LangGraph agent를 [AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)를 이용해 배포하고, Amazon ECS에 배포된 Web UI 애플리케이션에서 활용합니다. AWS 인프라는 루트 [installer.py](./installer.py)로 배포하고, LangGraph agent 이미지는 [Dockerfile](./runtime_agent/langgraph/Dockerfile)로 빌드한 뒤 [installer.py](./runtime_agent/langgraph/installer.py)로 AgentCore Runtime에 배포합니다. Web UI는 루트 [Dockerfile](./Dockerfile)로 ECS에 배포하며, Agent 추론은 AgentCore에서 수행합니다. 애플리케이션에서 AgentCore의 runtime을 호출할 때에는 [bedrock-agentcore](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-agentcore.html)의 [invoke_agent_runtime](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime.html)을 이용합니다. 이때에 각 agent를 생성할 때에 확인할 수 있는 [agentRuntimeArn](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_Agent.html)을 이용합니다. Agent는 [MCP](https://modelcontextprotocol.io/introduction)을 이용해 RAG, AWS Document, Tavily와 같은 검색 서비스를 활용할 수 있습니다. 여기에서는 RAG를 위하여 Lambda를 이용합니다. 데이터 저장소의 관리는 Knowledge base를 사용하고, 벡터 스토어로는 OpenSearch를 이용합니다. Agent에 필요한 S3, CloudFront, OpenSearch, Lambda등의 배포를 위해서는 AWS CDK를 이용합니다.


<img width="1000" alt="image" src="https://github.com/user-attachments/assets/5375df84-2f68-4041-942c-95cebc0e8c27" />


AgentCore의 runtime은 배포를 위해 Docker를 이용합니다. 현재(2025.7) 기준으로 arm64와 1GB 이하의 docker image를 지원합니다.
 
### Operation Architecture

Web UI(`application/server.py`, `application/web/`)에서 MCP·Skill·모델을 선택하면 `application/agentcore_client.py`가 AgentCore Runtime(`invoke_agent_runtime`)으로 요청을 보냅니다. 대화는 항상 Agent(Chat) 모드(`history_mode=Enable`)이며, New task마다 별도 `runtimeSessionId`로 checkpoint가 격리됩니다. Runtime은 `runtime_agent/langgraph/agent.py`의 `BedrockAgentCoreApp` 엔트리포인트에서 LangGraph 워크플로우를 실행하고, 선택된 MCP는 `runtime_agent/langgraph/mcp_config.py`에 따라 **동일 컨테이너 내 stdio 서브프로세스**로 기동됩니다. Skill은 `runtime_agent/langgraph/skills/`의 `SKILL.md`와 `get_skill_instructions` 도구로 제공되며, MCP와는 별도 체계입니다.

```mermaid
flowchart TB
  subgraph UI["Streamlit app.py"]
    MODE["Mode: Agent or Agent Chat"]
    SEL["Select MCP Skill Model"]
  end

  subgraph Client["agentcore_client.py"]
    RA[run_agent]
    RD["run_agent_in_docker local"]
  end

  subgraph Runtime["AgentCore runtime_agent/langgraph"]
    AG["agent.py BedrockAgentCoreApp"]
    CHAT["chat.py AsyncSqliteSaver bind_memory"]
    LGA["langgraph_agent.py StateGraph astream"]
  end

  subgraph BuiltIn["Built in tools"]
    LGB["execute_code bash read_file write_file upload_file_to_s3 get_current_time"]
  end

  subgraph Skills["Skills skill.py skills"]
    SKM[SkillManager]
    SKT[get_skill_instructions]
    SKD["docx pptx xlsx pdf skill_creator and more"]
  end

  subgraph MCPConfig["MCP config mcp_config.py"]
    LSC[load_selected_config]
  end

  subgraph MCPLocal["MCP servers stdio subprocess same container"]
    TV["tavily web search"]
    KB["knowledge base RAG retrieve"]
    AD["aws documentation uvx"]
    TI["trade info stock trend"]
    WF["web_fetch npx"]
    IG[image generation]
    UC["user config"]
  end

  subgraph MCPClient["langchain mcp adapters"]
    LGM[MultiServerMCPClient]
  end

  subgraph LLM["Amazon Bedrock runtime"]
    BR[Bedrock Runtime]
  end

  subgraph Storage["Artifacts and S3"]
    ART[artifacts]
    S3[(S3)]
  end

  MODE --> RA
  SEL --> RA
  RD -.-> AG

  RA --> AG
  AG --> CHAT
  CHAT --> LGA
  LGA --> BR
  LGA --> LGB
  LGA --> LGM
  LGA --> SKT

  SKT --> SKM
  SKM --> SKD

  AG --> LSC
  LSC --> MCPLocal
  LGM --> MCPLocal

  LGB --> ART
  LGB --> S3
```

| 모드 | 모듈 | 설명 |
|------|------|------|
| **Agent (Chat)** | `application/server.py` → `agentcore_client.run_agent` | 대화 이력 유지. `history_mode=Enable`, 태스크별 `runtimeSessionId` |
| LangGraph Runtime | `runtime_agent/langgraph/agent.py` | LangGraph StateGraph + `MultiServerMCPClient` + 내장 도구 |
| Skill | `runtime_agent/langgraph/skill.py` · `runtime_agent/langgraph/skills/` | `SKILL.md` 기반 지침. UI `application/skills.list`에서 선택 후 `get_skill_instructions`로 로드 |
| MCP (로컬 stdio) | `runtime_agent/langgraph/mcp_server_*.py` | Agent 컨테이너 안에서 subprocess로 기동 (`runtime_agent/langgraph/mcp_config.py`가 command/args 정의) |
| Web UI | 루트 `Dockerfile` → ECS | FastAPI + React SPA. Agent 추론은 AgentCore에서 수행 |

UI에서 MCP는 `application/mcp.list` 기준으로 선택하고, Skill은 `application/skills.list`에서 선택합니다. UI는 `agentcore_client.run_agent`로 AgentCore Runtime에 직접 요청합니다.

### 네트워크 설정

`langgraph-runtime`은 **ECS(Streamlit UI)** 와 **AgentCore Runtime(LangGraph 서버)** 가 모두 **private subnet** 에 배포됩니다. 이 환경에서는 인터넷으로 직접 나가지 않으므로, AWS API 호출은 **VPC Interface/Gateway Endpoint** 로, 외부 MCP·npm·cross-region 트래픽은 **NAT Gateway** 로 egress 를 열어야 합니다.

[installer.py](./installer.py) 가 신규 VPC 생성뿐 아니라 **기존 VPC 재사용 시**에도 아래 리소스를 자동으로 맞춥니다.

#### 구성 요약

```text
[사용자] → CloudFront → ALB (public subnet)
                              ↓
                    ECS App (private subnet)
                              ↓ bedrock-agentcore VPC Endpoint
                    AgentCore Runtime (private subnet, VPC mode)
                              ↓
              MCP: websearch (us-east-1 Gateway) / web_fetch (npm)
                              ↓ NAT Gateway (public subnet 경유)
                         Internet
```

| 구성 요소 | Subnet | 인터넷 egress |
|-----------|--------|----------------|
| ALB | Public | IGW |
| ECS Fargate | Private | VPC Endpoint + NAT |
| AgentCore Runtime | Private | VPC Endpoint + NAT |

#### VPC Interface Endpoint (us-west-2)

Private subnet 워크로드가 **같은 리전(us-west-2)** AWS API 에 도달할 때 사용합니다. `ensure_private_subnet_vpc_endpoints()` 가 생성·재사용합니다.

| AWS 서비스 | Endpoint 서비스 이름 | 용도 |
|------------|----------------------|------|
| Amazon ECR API | `com.amazonaws.us-west-2.ecr.api` | ECS/Runtime 이미지 pull 메타데이터 |
| Amazon ECR DKR | `com.amazonaws.us-west-2.ecr.dkr` | 컨테이너 이미지 레이어 pull |
| CloudWatch Logs | `com.amazonaws.us-west-2.logs` | ECS·Runtime 로그 전송 |
| Secrets Manager | `com.amazonaws.us-west-2.secretsmanager` | Runtime cold start 시 Tavily API 키 로드 ([runtime_agent/langgraph/utils.py](./runtime_agent/langgraph/utils.py)) |
| Bedrock AgentCore | `com.amazonaws.us-west-2.bedrock-agentcore` | ECS → `invoke_agent_runtime` |
| Bedrock AgentCore Control | `com.amazonaws.us-west-2.bedrock-agentcore-control` | Runtime ARN 검증, gateway 조회 |
| Amazon Bedrock Runtime | `com.amazonaws.us-west-2.bedrock-runtime` | LangGraph 모델 호출 (별도 생성) |
| Amazon S3 | `com.amazonaws.us-west-2.s3` (Gateway) | ECR 레이어·아티팩트·스토리지 |

Endpoint 는 private subnet 에 배치되며, ECS security group 과 Agent Runtime security group 모두 ingress(443) 를 허용해야 합니다.

#### NAT Gateway 와 private route table

아래 트래픽은 **VPC Endpoint 만으로는 처리할 수 없습니다.** Public subnet 에 **NAT Gateway** 를 두고, private subnet 전용 route table 에 `0.0.0.0/0 → NAT` 를 연결합니다 (`ensure_private_subnet_nat_routing()`).

| 트래픽 | 이유 |
|--------|------|
| **Websearch MCP** | Gateway 가 **us-east-1** (`gateway.bedrock-agentcore.us-east-1.amazonaws.com`) 에 있음. us-west-2 VPC Endpoint 로는 **다른 리전 API·Gateway HTTPS** 에 도달 불가 |
| **Websearch gateway URL 조회** | [runtime_agent/langgraph/mcp_config.py](./runtime_agent/langgraph/mcp_config.py) 가 `bedrock-agentcore-control` **us-east-1** API 호출 (`list_gateways` / `get_gateway`) |
| **Web_fetch MCP** | `npx -y mcp-server-fetch-typescript` 가 **npm registry** (`registry.npmjs.org`) 접속 필요 |
| **외부 URL fetch** | web_fetch·일반 HTTP 도구가 public 인터넷 대상에 접근 |

Websearch gateway 는 installer 가 `AGENTCORE_GATEWAY_REGION = "us-east-1"` 에 생성합니다. Runtime 은 us-west-2 VPC 에 있으므로 gateway 제어·데이터 평면 모두 **NAT egress** 가 필요합니다.

`application/config.json` 에 `agentcore_websearch_gateway_url` 이 있어도, gateway 에 **HTTPS로 연결**할 때는 여전히 NAT 가 필요합니다.

#### Websearch / Web_fetch 동작 경로

**Websearch** ([runtime_agent/langgraph/mcp_config.py](./runtime_agent/langgraph/mcp_config.py) → `websearch`):

1. (선택) `bedrock-agentcore-control` us-east-1 에서 gateway URL 조회  
2. `https://gateway-websearch-*.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp` 로 MCP streamable HTTP 연결 (SigV4)

**Web_fetch** (`mcp_config.py` → `web_fetch`):

1. `npx` 로 `mcp-server-fetch-typescript` 패키지 다운로드 (인터넷)  
2. 런타임 중 대상 URL HTTP fetch (인터넷)

채팅 UI 기본 MCP 가 `['websearch', 'web_fetch']` 이므로, **NAT 없이** 배포하면 MCP 초기화 단계에서 요청이 멈춘 것처럼 보일 수 있습니다. MCP 없이 동작 확인 시 payload 에 `mcp_servers: []` 를 사용할 수 있습니다.

#### installer 자동 설정

루트 [installer.py](./installer.py) 실행 시 네트워크 관련 단계:

1. **VPC** — public/private subnet, security group  
2. **NAT Gateway** — public subnet 에 생성, private subnet → `private-rt-{project}` 연결  
3. **VPC Endpoint** — 위 표의 Interface/Gateway Endpoint  
4. **Agent Runtime VPC** — Runtime 을 private subnet + 전용 SG 로 배포 (`networkMode: VPC`)  
5. **S3 Files** — 세션 스토리지(NFS)용 mount target  

기존 VPC 를 재사용해도 private subnet 이 이미 있으면 NAT·route table 연결을 **다시 검증·보완**합니다.

#### 증상별 점검

| 증상 | CloudWatch 로그 힌트 | 확인 사항 |
|------|----------------------|-----------|
| UI 는 열리나 채팅 무응답 | ECS: `agentcore_client` 이후 로그 없음 | `bedrock-agentcore`, `bedrock-agentcore-control` Endpoint |
| Runtime cold start 120초 초과 | Runtime: `utils.py` 까지만 반복 | `secretsmanager` Endpoint |
| MCP 로드 후 멈춤 | Runtime: `mcp_servers: ['websearch', 'web_fetch']` 이후 정지 | **NAT Gateway**, private route `0.0.0.0/0 → NAT` |
| Websearch 만 실패 | gateway us-east-1 관련 timeout | NAT + IAM(InvokeGateway) |

로그 그룹:

- ECS UI: `/ecs/app-for-langgraph-runtime`  
- Agent Runtime: `/aws/bedrock-agentcore/runtimes/runtime_langgraph-*-DEFAULT`

#### 비용 참고

- **VPC Interface Endpoint**: 시간당·데이터 처리 요금  
- **NAT Gateway**: 시간당 요금 + NAT 처리 데이터 요금 (websearch/web_fetch 사용 시 발생)

운영 환경에서 MCP 를 쓰지 않는다면 NAT 없이 VPC Endpoint 만으로도 기본 채팅(`mcp_servers: []`)은 가능합니다. Websearch·Web_fetch 를 쓰려면 NAT 구성을 권장합니다.

### AgentCore 소개

- AgentCore Runtime: AI agent와 tool을 배포하고 트래픽에 따라 자동으로 확장(Scaling)이 가능한 serverless runtime입니다. LangGraph, CrewAI, Strands Agents를 포함한 다양한 오픈소스 프레임워크을 지원합니다. 빠른 cold start, 세션 격리, 내장된 신원 확인(built-in identity), multimodal payload를 지원합니다. 이를 통해 안전하고 빠른 출시가 가능합니다.
- AgentCore Memory: Agent가 편리하게 short term, long term 메모리를 관리할 수 있습니다.
- AgentCore Code Interpreter: 분리된 sandbox 환경에서 안전하게 코드를 실행할 수 있습니다.
- AgentCore Broswer: 브라우저를 이용해 빠르고 안전하게 웹크롤링과 같은 작업을 수행할 수 있습니다.
- AgentCore Gateway: API, Lambda를 비롯한 서비스들을 쉽게 Tool로 활용할 수 있습니다.
- AgentCore Observability: 상용 환경에서 개발자가 agent의 동작을 trace, debug, monitor 할 수 있습니다.



## Agent 구현

AgentCore는 SSE 방식의 stream을 제공합니다. 

### LangGraph Agent

아래는 LangGraph로 구현한 ReAct agent입니다. 

```python
def buildChatAgentWithHistory(tools):
    tool_node = ToolNode(tools)

    workflow = StateGraph(State)

    workflow.add_node("agent", call_model)
    workflow.add_node("action", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "continue": "action",
            "end": END,
        },
    )
    workflow.add_edge("action", "agent")

    return workflow.compile(
        checkpointer=chat.checkpointer
    )
```


[runtime_agent/langgraph/agent.py](./runtime_agent/langgraph/agent.py)와 같이 stream 방식으로 처리하면 agent가 좀 더 동적으로 동작하게 할 수 있습니다. 아래와 같이 MCP 서버의 정보로 json 파일을 만든 후에 MultiServerMCPClient으로 client를 설정하고 나서 agent를 생성합니다. 이후 stream을 이용해 출력할때 json 형태의 결과값을 stream으로 전달합니다. 

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
app = BedrockAgentCoreApp()

@app.entrypoint
async def agent_langgraph(payload):
    mcp_json = mcp_config.load_selected_config(mcp_servers)
    server_params = load_multiple_mcp_server_parameters(mcp_json)
    client = MultiServerMCPClient(server_params)

    app = buildChatAgentWithHistory(tools)
    config = {
        "recursion_limit": 50,
        "configurable": {"thread_id": user_id},
        "tools": tools
    }    
    inputs = {
        "messages": [HumanMessage(content=query)]
    }
            
    value = None
    async for output in app.astream(inputs, config):
        for key, value in output.items():
            logger.info(f"--> key: {key}, value: {value}")

            if "messages" in value:
                for message in value["messages"]:
                    if isinstance(message, AIMessage):
                        yield({'data': message.content})
                        tool_calls = message.tool_calls
                        if tool_calls:
                            for tool_call in tool_calls:
                                tool_name = tool_call["name"]
                                tool_content = tool_call["args"]
                                toolUseId = tool_call["id"]
                                yield({'tool': tool_name, 'input': tool_content, 'toolUseId': toolUseId})
                    elif isinstance(message, ToolMessage):
                        toolResult = message.content
                        toolUseId = message.tool_call_id
                        yield({'toolResult': toolResult, 'toolUseId': toolUseId})
```

### Client

AgentCore로 agent_runtime_arn을 이용해 request에 대한 응답을 얻습니다. 이때 content-type이 "text/event-stream"인 경우에 prefix인 "data:"를 제거한 후에 json parser를 이용해 얻어진 값을 목적에 맞게 활용합니다.

```python
agent_core_client = boto3.client('bedrock-agentcore', region_name=bedrock_region)
response = agent_core_client.invoke_agent_runtime(
    agentRuntimeArn=agent_runtime_arn,
    runtimeSessionId=runtime_session_id,
    payload=payload,
    qualifier="DEFAULT" # DEFAULT or LATEST
)

result = current = ""
processed_data = set()  # Prevent duplicate data

# stream response
if "text/event-stream" in response.get("contentType", ""):
    for line in response["response"].iter_lines(chunk_size=10):
        line = line.decode("utf-8")        
        if line.startswith('data: '):
            data = line[6:].strip()  # Remove "data:" prefix and whitespace
            if data:  # Only process non-empty data
                # Check for duplicate data
                if data in processed_data:
                    continue
                processed_data.add(data)
                
                data_json = json.loads(data)
                if 'data' in data_json:
                    text = data_json['data']
                    logger.info(f"[data] {text}")
                    current += text
                    containers['result'].markdown(current)
                elif 'result' in data_json:
                    result = data_json['result']
                elif 'tool' in data_json:
                    tool = data_json['tool']
                    input = data_json['input']
                    toolUseId = data_json['toolUseId']
                    if toolUseId not in tool_info_list: # new tool info
                        tool_info_list[toolUseId] = index                                        
                        add_notification(containers, f"Tool: {tool}, Input: {input}")
                    else: # overwrite tool info
                        containers['notification'][tool_info_list[toolUseId]].info(f"Tool: {tool}, Input: {input}")                    
                elif 'toolResult' in data_json:
                    toolResult = data_json['toolResult']
                    toolUseId = data_json['toolUseId']
                    if toolUseId not in tool_result_list:  # new tool result
                        tool_result_list[toolUseId] = index
                        add_notification(containers, f"Tool Result: {toolResult}")
                    else: # overwrite tool result
                        containers['notification'][tool_result_list[toolUseId]].info(f"Tool Result: {toolResult}")
```



## 프로젝트 구조

프로젝트는 **Codex형 Web UI(`application/`)** 와 **LangGraph Agent Runtime(`runtime_agent/langgraph/`)** 으로 나뉩니다. 루트 [installer.py](./installer.py)는 ECS·VPC·Knowledge Base·**S3 Files 세션 스토리지**를 배포하고, [runtime_agent/langgraph/installer.py](./runtime_agent/langgraph/installer.py)는 AgentCore Runtime·ECR·IAM을 배포합니다. UI는 ECS에서 사용자 입력·MCP/Skill·모델 선택과 스트리밍 결과 표시만 담당하고, LLM 추론·MCP·Skill 실행·대화 checkpoint 저장은 AgentCore Runtime 컨테이너에서 수행합니다.

```text
Web UI (ECS)                            AgentCore Runtime
application/server.py                   runtime_agent/langgraph/agent.py
application/web/ (React)                        │
        │                                         ▼
        ▼                                 langgraph_agent.py
application/agentcore_client.py  ──SSE──▶  chat.py · skill.py · mcp_config.py
  invoke_agent_runtime
```

### `application/` — Codex형 Web UI (ECS)

루트 [Dockerfile](./Dockerfile)로 빌드되어 ECS에 배포됩니다. FastAPI + React SPA이며, AgentCore Runtime을 `invoke_agent_runtime`으로 호출합니다.

```text
application/
├── server.py               # FastAPI 진입점, SPA 정적 파일 서빙
├── task_store.py           # 태스크·메시지 SQLite 저장
├── api/                    # REST + SSE API
├── web/                    # React + Vite 프론트엔드
├── agentcore_client.py     # AgentCore Runtime 호출 (invoke_agent_runtime, SSE 파싱)
├── chat.py                 # UI 측 모델 선택 상태
├── info.py                 # Bedrock/OpenAI 모델 ID·리전·Mantle API 매핑
├── utils.py                # config.json 로드, 공통 유틸
├── notification_queue.py   # SSE 스트리밍 알림 큐
├── bedrock_data_retention.py
├── mcp.list
├── skills.list
└── config.json
```

| 파일 | 역할 |
|------|------|
| `server.py` | FastAPI 앱, `/api/*` REST·SSE, React SPA 서빙 |
| `task_store.py` | New task별 `runtime_session_id`·UI 메시지 영속 |
| `agentcore_client.py` | payload를 Runtime으로 전송, SSE 스트림 처리. 태스크별 `runtime_session_id` 지원 |
| `web/` | Codex형 사이드바(New task, Skill, MCP, Model) + 채팅 UI |

## App UI

Web UI는 **FastAPI 백엔드 + React SPA**로 구성됩니다. Streamlit을 대체한 Codex형 레이아웃이며, ECS(또는 로컬 `8501`)에서 `application/server.py`가 API와 빌드된 정적 파일(`application/web/dist/`)을 함께 제공합니다.

### 기술 스택

| 구분 | 기술 | 용도 |
|------|------|------|
| **백엔드** | FastAPI, uvicorn | REST API, SSE 스트리밍, SPA 정적 파일 서빙 |
| **백엔드** | SQLite (`task_store.py`) | User별 task·메시지·`runtime_session_id` 영속 |
| **백엔드** | `agentcore_client.py` | AgentCore Runtime `invoke_agent_runtime` 호출 |
| **프론트엔드** | React 19, TypeScript | SPA UI |
| **프론트엔드** | Vite 6 | 개발 서버·프로덕션 빌드 |
| **프론트엔드** | react-markdown, remark-gfm | Assistant 응답 Markdown 렌더링 |
| **프론트엔드** | CSS (`codex.css`) | 다크 테마 Codex형 레이아웃 |
| **인증** | HttpOnly Cookie (`agent_user_id`) | User ID 세션 유지 |

### 화면 구조

```text
┌─────────────────────────────────────────────────────────────┐
│ UserIdModal (최초 진입 · 쿠키 없음)                          │
│   User ID 입력 → /api/session POST                           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────┬──────────────────────────────────────────────┐
│ Sidebar      │ Main Panel                                   │
│              │                                              │
│ • Brand      │ ChatThread                                   │
│   (project   │   • task 제목 헤더                           │
│    Name)     │   • MessageBubble (user / assistant)         │
│ • New task   │   • ToolCallCard (tool / tool_result)        │
│ • Task list  │   • streaming indicator                      │
│ • Skill (N)  │                                              │
│ • MCP (N)    │ ChatInput                                    │
│ • Model      │   • 메시지 입력 · 전송                       │
│ • Guardrail  │                                              │
└──────────────┴──────────────────────────────────────────────┘
        │
        └── ConfigDrawer (Skill / MCP 다중 선택)
```

| 영역 | 컴포넌트 | 설명 |
|------|----------|------|
| 인증 | `UserIdModal` | User ID 입력 후 쿠키 세션 생성 |
| 사이드바 | `Sidebar`, `TaskListItem` | 태스크 목록, New task, 핀·이름 변경·삭제 |
| 설정 | `ConfigDrawer` | Skill·MCP 체크박스 선택 (태스크별) |
| 채팅 | `ChatThread`, `MessageBubble`, `ChatInput` | 대화 스레드, Markdown·도구 이벤트, 입력 |
| 스트리밍 | `useChatStream` | SSE 이벤트(`token`, `tool`, `tool_result`, `done`) 처리 |

사이드바 상단 **Brand**와 브라우저 탭 제목은 `config.json`의 `projectName`을 사용합니다. 하이픈(`-`)은 공백으로 바꾸고 첫 글자만 대문자로 표시합니다. (예: `agentic-work` → `Agentic work`)

### 프론트엔드 디렉터리 (`application/web/`)

```text
application/web/
├── index.html
├── package.json
├── vite.config.ts
├── src/
│   ├── main.tsx              # React 진입점
│   ├── App.tsx               # 세션·태스크·채팅 상태 관리
│   ├── api.ts                # /api/* fetch·SSE 클라이언트
│   ├── types.ts              # Task, Message, AppConfig 타입
│   ├── formatBrandTitle.ts   # projectName → Brand/탭 제목
│   ├── hooks/
│   │   └── useChatStream.ts  # 채팅 SSE 스트림 훅
│   ├── components/
│   │   ├── UserIdModal.tsx
│   │   ├── Sidebar.tsx
│   │   ├── TaskListItem.tsx
│   │   ├── ConfigDrawer.tsx
│   │   ├── ChatThread.tsx
│   │   ├── MessageBubble.tsx
│   │   ├── ChatInput.tsx
│   │   └── ToolCallCard.tsx
│   └── styles/
│       └── codex.css
└── dist/                     # npm run build 결과 (server.py가 서빙)
```

### REST / SSE API

| Method | Path | 설명 |
|--------|------|------|
| `GET` | `/api/health` | 헬스체크 |
| `GET`/`POST` | `/api/session` | User ID 세션 조회·생성 (Cookie) |
| `GET` | `/api/config` | Skill·MCP·Model 목록 및 기본값 |
| `PATCH` | `/api/config/defaults` | 기본 Skill·MCP 저장 |
| `GET`/`POST` | `/api/tasks` | 태스크 목록·생성 |
| `GET`/`PATCH`/`DELETE` | `/api/tasks/{id}` | 태스크 조회·수정·삭제 |
| `GET` | `/api/tasks/{id}/messages` | 태스크 메시지 목록 |
| `POST` | `/api/tasks/{id}/chat` | 채팅 SSE 스트림 (`data: {...}`) |

채팅 요청은 `agentcore_client.run_agent` → AgentCore Runtime으로 전달되며, 태스크마다 고유한 `runtime_session_id`로 checkpoint가 격리됩니다.

### 로컬 개발

```text
# 1) 프론트엔드 빌드 (최초 1회 또는 UI 변경 후)
cd application/web
npm install
npm run build

# 2) 백엔드 실행
cd ../..
uvicorn application.server:app --host 0.0.0.0 --port 8501
```

프론트엔드 핫 리로드 개발 시 Vite dev server(`5173`)를 사용합니다. `/api`는 `vite.config.ts`에서 `8501`로 프록시됩니다.

```text
cd application/web
npm run dev
```

### `runtime_agent/langgraph/` — LangGraph Agent (AgentCore Runtime)

[runtime_agent/langgraph/Dockerfile](./runtime_agent/langgraph/Dockerfile)로 arm64 이미지를 빌드하고, [runtime_agent/langgraph/installer.py](./runtime_agent/langgraph/installer.py)로 AgentCore Runtime·IAM·ECR을 배포합니다.

```text
runtime_agent/langgraph/
├── agent.py                # BedrockAgentCoreApp 엔트리포인트, payload 파싱·스트리밍 응답
├── langgraph_agent.py      # LangGraph StateGraph, LLM 호출, 도구 바인딩
├── chat.py                 # LLM 빌드(Bedrock/Mantle), MCP 클라이언트, AsyncSqliteSaver checkpoint
├── info.py                 # Runtime 모델 ID·리전·mantle_api 매핑 (application/info.py와 동일)
├── skill.py                # SkillManager, get_skill_instructions 도구
├── mcp_config.py           # 선택된 MCP → stdio subprocess / Gateway URL 매핑
├── mcp_server_retrieve.py  # Knowledge Base retrieve MCP
├── mcp_server_trade_info.py
├── mcp_server_image_generation.py
├── mcp_server_korea_weather.py
├── mcp_retrieve.py         # retrieve MCP 헬퍼
├── trade_info.py           # trade info 데이터 로더
├── agentcore_sigv4_auth.py # AgentCore Gateway MCP용 SigV4 httpx Auth
├── bedrock_data_retention.py  # Mantle bearer token, data retention opt-in
├── utils.py                # config 로드, Tavily API key(Secrets Manager) 등
├── installer.py            # AgentCore Runtime·IAM·ECR 배포
├── uninstaller.py          # Runtime·IAM·ECR 삭제
├── test_runtime_remote.py  # Runtime 원격 invoke 테스트
├── mcp.list                # 지원 MCP 목록
├── skills.list             # 지원 Skill 목록
├── mcp.env                 # 사용자 설정 MCP 환경 변수 예시
├── Dockerfile              # AgentCore Runtime 컨테이너 이미지
├── config.json             # Knowledge Base ID, region, projectName 등
└── skills/                 # Skill 정의 (아래 참조)
    ├── docx/
    ├── pdf/
    ├── pptx/
    ├── xlsx/
    ├── skill-creator/
    ├── subway/             # skills.list의 seoul-subway
    ├── usa-weather/
    └── kma-weather/
```

| 구분 | 모듈 | 설명 |
|------|------|------|
| **엔트리포인트** | `agent.py` | AgentCore 요청 수신 → `chat.update`로 모델·user_id 반영 → `langgraph_agent` 실행 |
| **추론·메모리** | `langgraph_agent.py`, `chat.py` | StateGraph agent/tool 루프, Bedrock·Mantle LLM, `/mnt/workspace` SQLite checkpoint |
| **MCP** | `mcp_config.py`, `mcp_server_*.py` | UI에서 선택된 MCP를 stdio subprocess 또는 AgentCore Gateway로 기동 |
| **Skill** | `skill.py`, `skills/` | `SKILL.md` 기반 지침. `get_skill_instructions` 도구로 로드 |
| **인증·모델** | `agentcore_sigv4_auth.py`, `bedrock_data_retention.py`, `info.py` | Gateway SigV4, Mantle bearer token, 모델 프로필 |
| **설정·배포** | `utils.py`, `installer.py`, `config.json` | AWS 리소스 연동, Secrets Manager, Runtime/IAM 배포 |

**MCP 목록 (`mcp.list`)**: knowledge base, aws documentation, trade info, websearch, web_fetch, image generation, 사용자 설정

**Skill 목록 (`skills.list`)**: docx, pdf, pptx, xlsx, skill-creator, seoul-subway, usa-weather, kma-weather

> OpenAI GPT 5.4/5.5는 Bedrock Mantle Responses API(`mantle_api: "responses"`)를 사용합니다. Runtime IAM 정책(`installer.py`의 `BedrockMantleAccess`)에 모델이 호출하는 Mantle 리전(예: `us-east-2`)이 포함되어야 합니다.

### Skill 구조 (`runtime_agent/langgraph/skills/`)

각 Skill은 `SKILL.md` 파일이 핵심이며, 필요에 따라 `scripts/`, `references/`, `assets/` 등의 보조 폴더를 포함할 수 있습니다. `application/skills.list`의 이름과 `runtime_agent/langgraph/skills/` 하위 디렉터리가 대응합니다. (`seoul-subway` → `subway/`)

```text
skills/
├── docx/
│   ├── SKILL.md          # YAML 프론트매터 + 상세 지침
│   └── scripts/          # 문서 처리 스크립트
├── pptx/
│   └── SKILL.md
├── xlsx/
│   └── SKILL.md
├── pdf/
│   └── SKILL.md
├── skill-creator/
│   └── SKILL.md
├── subway/               # seoul-subway
│   └── SKILL.md
├── usa-weather/
│   └── SKILL.md
└── kma-weather/
    ├── SKILL.md
    └── scripts/
```

## Runtime Agent

LangGraph agent는 [runtime_agent/langgraph/](./runtime_agent/langgraph/)에 구현되어 있으며, AgentCore Runtime 컨테이너에서 `agent.py`의 `BedrockAgentCoreApp` 엔트리포인트로 실행됩니다.

### IAM 인증

LangGraph agent에 대한 이미지를 [runtime_agent/langgraph/Dockerfile](./runtime_agent/langgraph/Dockerfile)을 이용해 빌드후 ECR에 배포합니다. 또한, Agent Runtime 배포 시 IAM 인증을 사용합니다. [create_agent_runtime](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-agentcore-control/client/create_agent_runtime.html)에서 authorizerConfiguration을 포함하지 않은 경우에 IAM으로 인증하게 됩니다. Runtime 생성시 client는 bedrock-agentcore-control을 사용하고 Agent 이미지에 대한 ECR 경로를 가지고 있어야 합니다. 

Agent에서 외부 AgentCore endpoint로 요청을 보낼때에는 아래와 같이 IAM 인증을 수행하기 위하여 request에 X-Amz-Security-Token을 포함합니다. 이를 위해 httpx의 event hook을 이용해 아래와 같이 구현할 수 있습니다. 상세코드는 [runtime_agent/langgraph/agent.py](./runtime_agent/langgraph/agent.py)을 참조합니다.

```python
original_init = httpx.AsyncClient.__init__
def patched_init(self, *args, **kwargs):
    # Add SigV4 signing event hook if needed
    async def sign_request(request: httpx.Request) -> None:
        """Sign the request with AWS SigV4 including the body"""
        # Only sign requests to bedrock-agentcore
        if "bedrock-agentcore" not in str(request.url):
            return
        
        # Get credentials
        boto_session = boto3.Session()
        credentials = boto_session.get_credentials().get_frozen_credentials()
        
        # Parse URL
        parsed_url = urlparse(str(request.url))
        host = parsed_url.netloc
        
        # Generate timestamp
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        
        # Read request body if available
        body = None
        if request.content:
            if isinstance(request.content, bytes):
                body = request.content
            else:
                try:
                    body = await request.aread()
                    if hasattr(request, '_content'):
                        request._content = body
                except Exception:
                    pass
        
        # Create AWS request headers
        aws_headers = {
            'host': host,
            'x-amz-date': timestamp,
            'Content-Type': request.headers.get('Content-Type', 'application/json'),
            'Accept': request.headers.get('Accept', 'application/json, text/event-stream')
        }
        
        if body:
            aws_headers['Content-Length'] = str(len(body))
        
        # Create AWS request for signing
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            headers=aws_headers,
            data=body
        )
        
        # Sign the request
        region = utils.load_config().get("region", "us-west-2")
        auth = BotocoreSigV4Auth(credentials, "bedrock-agentcore", region)
        auth.add_auth(aws_request)
        
        # Update request headers
        request.headers['X-Amz-Date'] = timestamp
        request.headers['Authorization'] = aws_request.headers['Authorization']
        
        if credentials.token:
            request.headers['X-Amz-Security-Token'] = credentials.token
    
    # Add event_hooks to kwargs if not already present
    if 'event_hooks' not in kwargs:
        kwargs['event_hooks'] = {'request': [], 'response': []}
    elif not isinstance(kwargs['event_hooks'], dict):
        kwargs['event_hooks'] = {'request': [], 'response': []}
    
    if 'request' not in kwargs['event_hooks']:
        kwargs['event_hooks']['request'] = []
    
    # Add the sign_request hook
    kwargs['event_hooks']['request'].append(sign_request)

    # Call original init with modified kwargs
    original_init(self, *args, **kwargs)
```

Streamlit에서 입력하면 AgentCore endpoint로 전달되는데 이때에 아래와 같이 BedrockAgentCoreApp의 entrypoint로 받아서 실행합니다.

```python
import httpx
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@app.entrypoint
async def agent_langgraph(payload):
    httpx.AsyncClient.__init__ = patched_init
    
    client = MultiServerMCPClient(server_params)
    tools = await client.get_tools()
    
    app = langgraph_agent.buildChatAgentWithHistory(tools)
    config = {
        "recursion_limit": 50,
        "configurable": {"thread_id": user_id},
        "tools": tools,
        "system_prompt": None
    }
    
    inputs = {"messages": [HumanMessage(content=query)]}
            
    value = final_output = None
    async for output in app.astream(inputs, config):
        for key, value in output.items():
            logger.info(f"--> key: {key}, value: {value}")

            if key == "messages" or key == "agent":
                if isinstance(value, dict) and "messages" in value:
                    final_output = value
                elif isinstance(value, list):
                    final_output = {"messages": value, "image_url": []}
                else:
                    final_output = {"messages": [value], "image_url": []}
```


## Session Storage

AgentCore Runtime에서 대화 context를 유지하려면 **Session Storage**를 사용합니다. 이 프로젝트는 배포 후에도 checkpoint를 유지하기 위해 **Amazon S3 Files**를 `/mnt/workspace`에 마운트하고, LangGraph **AsyncSqliteSaver**가 `langgraph_checkpoints.sqlite`에 대화 이력을 저장합니다. (`s3_files_access_point_arn`이 없으면 managed `sessionStorage` + `PUBLIC` 모드로 fallback합니다.)

### Runtime 생성 시 filesystem 설정

[runtime_agent/langgraph/installer.py](./runtime_agent/langgraph/installer.py)의 `create_agent_runtime_func()` / `update_agent_runtime_func()`에서 runtime을 생성·갱신할 때 `/mnt/workspace`를 마운트합니다. (`/mnt/` 하위 경로 필수)

#### S3 Files를 이용하는 경우

- **기본 (S3 Files)**: `s3FilesAccessPoint` + `networkMode: VPC`
- **fallback**: `sessionStorage` + `networkMode: PUBLIC` (`s3_files_access_point_arn` 없을 때)

아래는 **S3 Files 모드(기본)** 의 전체 `create_agent_runtime` 호출 예시입니다. `config`에는 루트 [installer.py](./installer.py)가 `application/config.json`에 기록한 S3 Files·VPC 키가 들어 있습니다.

```python
import boto3

client = boto3.client("bedrock-agentcore-control", region_name=config["region"])

response = client.create_agent_runtime(
    agentRuntimeName=runtime_name,  # 예: langgraph_runtime_langgraph
    agentRuntimeArtifact={
        "containerConfiguration": {
            "containerUri": (
                f"{config['accountId']}.dkr.ecr.{config['region']}"
                f".amazonaws.com/{repository_name}:{image_tag}"
            )
        }
    },
    filesystemConfigurations=[
        {
            "s3FilesAccessPoint": {
                "accessPointArn": config["s3_files_access_point_arn"],
                "mountPath": "/mnt/workspace",
            }
        }
    ],
    networkConfiguration={
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": config["agent_runtime_vpc_subnets"],
            "securityGroups": config["agent_runtime_security_groups"],
        },
    },
    roleArn=config["agent_runtime_role"],
)

print(response["agentRuntimeArn"])
```

#### Runtime의 Session Storage를 사용하는 경우

Runtime이 가지고 있는 managed session storage만 쓸 때의 형태는 아래와 같습니다. Session Storage는 추가 요청이 없을때에도 2주간 저장되고 세션당 1G까지 저장됩니다. 다만, Runtime 재배포로 version이 업데이트되면, 세션이 초기화되므로, 세션 정보가 애플리케이션의 목적에 중요하다면, S3 Files를 권장합니다.

```python
response = client.create_agent_runtime(
    agentRuntimeName=runtime_name,
    agentRuntimeArtifact={
        "containerConfiguration": {
            "containerUri": f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/{repository_name}:{image_tag}"
        }
    },
    filesystemConfigurations=[
        {
            "sessionStorage": {
                "mountPath": "/mnt/workspace",
            }
        }
    ],
    networkConfiguration={"networkMode": "PUBLIC"},
    roleArn=agent_runtime_role,
)
```

`update_agent_runtime`에도 **동일한** `filesystemConfigurations`와 `networkConfiguration`을 포함해야 합니다. update 시 누락하면 cold start마다 checkpoint가 사라질 수 있습니다.

### LangGraph checkpointer 연동

기존 `MemorySaver`는 프로세스 메모리에만 저장되어 컨테이너가 재시작되면 history가 사라집니다. `history_mode=Enable`일 때 [runtime_agent/langgraph/chat.py](./runtime_agent/langgraph/chat.py)의 `ensure_checkpointer()`가 **AsyncSqliteSaver**를 초기화하고, `buildChatAgentWithHistory()`가 이를 checkpointer로 사용합니다.

| 구분 | Strands (참고) | LangGraph (본 프로젝트) |
|------|----------------|-------------------------|
| 저장소 | `FileSessionManager(storage_dir="/mnt/workspace")` | `AsyncSqliteSaver` → `/mnt/workspace/langgraph_checkpoints.sqlite` |
| 세션 키 | `session_id` | `config["configurable"]["thread_id"]` |

```python
# chat.py — 요약
SESSION_STORAGE_DIR = os.environ.get("SESSION_STORAGE_DIR", "/mnt/workspace")
CHECKPOINT_DB = os.path.join(SESSION_STORAGE_DIR, "langgraph_checkpoints.sqlite")

async def ensure_checkpointer():
    saver = AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB)
    checkpointer = await saver.__aenter__()
    await checkpointer.setup()
    return checkpointer
```

`buildChatAgentWithHistory()`는 아래와 같이 checkpointer를 compile 시 전달합니다.

```python
return workflow.compile(
    checkpointer=chat.checkpointer
)
```



### 클라이언트 runtimeSessionId

Web UI 클라이언트([application/agentcore_client.py](./application/agentcore_client.py))는 **태스크별 `runtimeSessionId`**를 사용합니다. New task를 만들면 새 UUID가 부여되어 AgentCore checkpoint가 격리됩니다.

```python
def runtime_session_id_for(user_id: str, history_mode: str) -> str:
    if history_mode == "Enable" and user_id:
        seed = f"agentcore-session-{user_id}"
        session_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    else:
        session_id = str(uuid.uuid4())
    return session_id
```

```mermaid
sequenceDiagram
    participant UI as Streamlit
    participant Client as agentcore_client
    participant AC as AgentCore Runtime
    participant LG as LangGraph

    UI->>Client: history_mode=Enable, user_id
    Client->>AC: invoke(runtimeSessionId=uuid5(user_id))
    Note over AC: /mnt/workspace 마운트
    AC->>LG: astream(..., thread_id=user_id:scope)
    LG->>LG: AsyncSqliteSaver → langgraph_checkpoints.sqlite
    Client->>AC: 다음 턴 (동일 runtimeSessionId)
    LG->>LG: thread_id로 이전 checkpoint 로드
```

### 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `SESSION_STORAGE_DIR` | `/mnt/workspace` | checkpoint SQLite 디렉터리 |
| `SESSION_STORAGE_ENABLED` | `true` | `false`이면 `MemorySaver`로 폴백 |

로컬에서 session storage 없이 실행할 때는 `SESSION_STORAGE_DIR`이 없으면 `runtime_agent/langgraph/.session_storage`를 사용합니다.

### 주의사항

- **세션 범위**: Managed `sessionStorage`는 14일 idle·Version 업데이트 시 초기화될 수 있습니다. 이 프로젝트는 **S3 Files**로 `/mnt/workspace`를 영속화하므로 배포 후에도 checkpoint가 유지됩니다. ([S3 Files 활용](#s3-files-활용) 참조)
- **요청마다 agent 재생성**: `agent.py`는 매 요청 `create_agent()`를 호출하지만, checkpointer가 파일에 있으면 `thread_id`만 같으면 history를 복원합니다.
- **`InMemoryStore`는 휘발성**: `store=chat.memorystore`는 LangGraph Store API용이며 메모리에만 있습니다. 대화 history만 필요하면 checkpointer만으로 충분합니다.
- **의존성**: [runtime_agent/langgraph/Dockerfile](./runtime_agent/langgraph/Dockerfile)에 `langgraph-checkpoint-sqlite`, `aiosqlite`가 포함되어 있습니다.



### S3 Files 활용

Managed `sessionStorage`는 Runtime **Version 업데이트 시 `/mnt/workspace` 데이터가 초기화**됩니다. LangGraph checkpoint(`langgraph_checkpoints.sqlite`)를 배포 후에도 유지하려면, 이 프로젝트는 **Amazon S3 Files**를 bring-your-own 파일시스템으로 마운트합니다.

| 항목 | Managed `sessionStorage` | S3 Files `s3FilesAccessPoint` (현재 기본) |
|------|--------------------------|-------------------------------------------|
| API 키 | `sessionStorage` | `s3FilesAccessPoint` |
| Network | `PUBLIC` | `VPC` (private subnet 필수) |
| Version 업데이트 후 | checkpoint 삭제 | **유지** |
| 실제 저장소 | AgentCore managed | S3 bucket `agentcore-sessions/` prefix |
| LangGraph 코드 | `SESSION_STORAGE_DIR=/mnt/workspace` | 동일 (변경 없음) |

#### 전체 아키텍처

```mermaid
flowchart TB
    subgraph root_installer ["installer.py (루트)"]
        A[create_vpc] --> B[create_s3_files_session_storage]
        B --> C[apply_s3_files_config → application/config.json]
    end

    subgraph s3files_aws ["S3 Files (AWS)"]
        D[S3 bucket / agentcore-sessions/]
        E[File System + Mount Targets]
        F[Access Point]
    end

    subgraph runtime_installer ["runtime_agent/langgraph/installer.py"]
        G[create_agent_runtime]
        H["s3FilesAccessPoint @ /mnt/workspace"]
        I["networkMode: VPC"]
    end

    subgraph runtime_agent ["chat.py / langgraph_agent.py"]
        J["AsyncSqliteSaver → langgraph_checkpoints.sqlite"]
    end

    B --> D
    B --> E
    B --> F
    C --> G
    G --> H
    G --> I
    H --> J
    F --> J
```

#### 배포 흐름 (`installer.py`)

VPC 생성 직후 `[5.5/10] Creating S3 Files session storage` 단계에서 다음을 **멱등**으로 프로비저닝합니다.

1. **`_get_or_create_s3files_sync_role()`** — S3 ↔ NFS 동기화용 IAM role (`elasticfilesystem.amazonaws.com` trust)
2. **`_get_or_create_s3files_file_system()`** — `agentcore-sessions/` prefix, bucket versioning `Enabled` 필수
3. **Security groups** — runtime SG ↔ mount target SG, NFS **TCP 2049**
4. **`_ensure_s3files_mount_targets()`** — private subnet별 mount target
5. **`_get_or_create_s3files_access_point()`** — POSIX `uid/gid: 0/0`
6. **`_ensure_s3files_file_system_policy()`** — Runtime 실행 역할에 NFS `ClientMount` / `ClientWrite` 허용 (resource-based policy)
7. **`_add_security_group_to_vpc_endpoint()`** — Bedrock VPC endpoint에 runtime SG 추가

`application/config.json`에 저장되는 키:

```json
{
  "s3_files_file_system_id": "fs-xxxxxxxx",
  "s3_files_access_point_arn": "arn:aws:s3files:...",
  "agent_runtime_vpc_subnets": ["subnet-aaa", "subnet-bbb"],
  "agent_runtime_security_groups": ["sg-runtime-xxx"]
}
```

#### AgentCore Runtime 연결 (`runtime_agent/langgraph/installer.py`)

`load_config()` → `_merge_application_config()`로 위 키를 runtime `config.json`에 동기화합니다.

```python
def session_storage_filesystem_configurations(config: dict):
    access_point_arn = config.get("s3_files_access_point_arn")
    if access_point_arn:
        return [{
            "s3FilesAccessPoint": {
                "accessPointArn": access_point_arn,
                "mountPath": "/mnt/workspace",
            }
        }]
    return [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]

def agent_runtime_network_configuration(config: dict):
    if not config.get("s3_files_access_point_arn"):
        return {"networkMode": "PUBLIC"}
    return {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": config["agent_runtime_vpc_subnets"],
            "securityGroups": config["agent_runtime_security_groups"],
        },
    }
```

`create_bedrock_agentcore_policy()`에 S3 Files mount 권한이 조건부로 추가됩니다. `s3files:GetAccessPoint`는 **access point ARN**을 Resource로 지정해야 `update_agent_runtime` 시 `ValidationException`이 발생하지 않습니다. `s3files:ListMountTargets`도 Runtime 생성·갱신 검증에 필요합니다.

```python
file_system_arn = f"arn:aws:s3files:{region}:{accountId}:file-system/{file_system_id}"

# Client mount/write (file system ARN + access point condition)
{
    "Sid": "S3FilesClientAccess",
    "Effect": "Allow",
    "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
    "Resource": file_system_arn,
    "Condition": {
        "ArnEquals": {"s3files:AccessPointArn": "{access_point_arn}"}
    },
}
# GetAccessPoint (access point ARN)
{
    "Sid": "S3FilesGetAccessPoint",
    "Effect": "Allow",
    "Action": ["s3files:GetAccessPoint"],
    "Resource": "{access_point_arn}",
}
# ListMountTargets (file system ARN)
{
    "Sid": "S3FilesListMountTargets",
    "Effect": "Allow",
    "Action": ["s3files:ListMountTargets"],
    "Resource": file_system_arn,
}
```

**S3 Files file system policy** — 루트 `installer.py`의 `_ensure_s3files_file_system_policy()`가 file system에 resource-based policy를 설정합니다. 실행 역할 IAM만으로는 NFS 쓰기가 허용되지 않을 수 있습니다.

```python
{
    "Effect": "Allow",
    "Principal": {
        "AWS": "arn:aws:iam::{accountId}:role/AmazonBedrockAgentCoreRuntimeRoleFor{project_name}"
    },
    "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
    "Condition": {
        "StringEquals": {"s3files:AccessPointArn": "{access_point_arn}"}
    },
}
```

배포 로그에서 아래 메시지로 S3 Files 모드 적용 여부를 확인할 수 있습니다.

```text
Session storage: S3 Files access point at /mnt/workspace (VPC mode)
✓ s3FilesAccessPoint verified: mountPath=/mnt/workspace, arn=arn:aws:s3files:...
```

#### LangGraph checkpointer 연동

Runtime Agent 코드는 마운트 경로만 `/mnt/workspace`이면 되므로 **변경하지 않습니다**.

```python
# runtime_agent/langgraph/chat.py
SESSION_STORAGE_DIR = os.environ.get("SESSION_STORAGE_DIR", "/mnt/workspace")
CHECKPOINT_DB = os.path.join(SESSION_STORAGE_DIR, "langgraph_checkpoints.sqlite")
```

S3 측 경로: `s3://{bucket}/agentcore-sessions/` (예: `langgraph_checkpoints.sqlite`가 이 prefix 아래에 동기화됨. NFS → S3 동기화 지연 ~60초).

#### 적용·재배포

```bash
# 전체 인프라 + S3 Files + Runtime
cd langgraph-runtime
python3 installer.py

# Runtime만 S3 Files 모드로 갱신 (config에 S3 Files 키 필요)
python3 runtime_agent/langgraph/installer.py
```

기존 Runtime이 `PUBLIC` + `sessionStorage`로 만들어져 있다면, runtime installer 재실행 시 `update_agent_runtime`으로 S3 Files + VPC 모드로 업데이트됩니다.

#### 주의사항

- S3 Files는 **VPC 모드 전용**입니다. SG(2049)·AZ 정렬이 맞지 않으면 invoke 시 HTTP 424가 날 수 있습니다.
- S3 bucket **versioning은 `Enabled`** 여야 합니다 (`_ensure_s3_bucket_versioning_enabled`).
- `s3_files_access_point_arn`이 config에 없으면 installer는 **Managed `sessionStorage` + PUBLIC** 으로 fallback합니다.
- Managed `sessionStorage`는 Version 업데이트·14일 idle 시 checkpoint가 사라질 수 있습니다. 운영 환경에서는 S3 Files 사용을 권장합니다.
- S3 Files는 NFS 기반이므로 S3 API로 즉시 읽어야 하는 downstream에는 동기화 지연(~60초)을 고려해야 합니다.
- access point POSIX UID/GID는 컨테이너 실행 사용자와 일치해야 합니다. 현재 구현은 `uid/gid: 0/0`(root)입니다.
- checkpoint·세션 파일은 버킷 루트가 아니라 **`agentcore-sessions/`** prefix 아래에 동기화됩니다. 콘솔에서 prefix로 확인하세요.
- **트러블슈팅**
  - S3 bucket이 비어 있고 Runtime이 `PUBLIC` + `sessionStorage`이면 S3 Files 마운트가 적용되지 않은 것입니다. `python3 runtime_agent/langgraph/installer.py`로 Runtime을 재배포하세요.
  - `update_agent_runtime` 시 `Ensure the role has s3files:GetAccessPoint` → 실행 역할 IAM에서 `GetAccessPoint` Resource를 access point ARN으로 분리했는지 확인하세요.
  - `/mnt/workspace`에 `Permission denied` → `_ensure_s3files_file_system_policy()`가 적용됐는지, `s3files:ClientWrite`가 file system policy에 있는지 확인하세요.






### 세션 관리

AgentCore Runtime에서 대화 history를 유지하려면 **`/mnt/workspace` 영속 마운트**(S3 Files 또는 managed `sessionStorage`), **동일한 `runtimeSessionId`**, LangGraph **checkpointer**(SQLite)가 함께 동작해야 합니다. 상세 구현은 위 [Session Storage](#session-storage) 및 [S3 Files 활용](#s3-files-활용) 절을 참조합니다.

#### sessionStorage (managed session storage)

[AWS 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)에 따르면, `sessionStorage`는 **runtimeSessionId마다** 격리된 persistent 디렉터리(`/mnt/workspace` 등)를 제공합니다. agent가 일반 파일 I/O로 쓴 내용은 서비스가 durable storage에 비동기 복제하고, microVM이 stop/resume(cold start)되어도 **같은 `runtimeSessionId`로 invoke하면** 파일 상태가 복원됩니다.

| 항목 | 내용 |
|------|------|
| 설정 위치 | `create_agent_runtime` / `update_agent_runtime`의 `filesystemConfigurations` |
| mount path | `/mnt/` 하위 1단계 필수 (예: `/mnt/workspace`) |
| 세션 격리 | `runtimeSessionId`마다 별도 storage (세션 간 공유 불가) |
| session당 용량 | 최대 1 GB |
| idle 만료 | **14일**간 invoke 없으면 데이터 삭제 |
| version 업데이트 | **agent runtime version 변경 시 session data 초기화** |

**stop/resume lifecycle (AWS):**

1. 첫 invoke — microVM 생성, mount path는 빈 디렉터리
2. agent write — 로컬 파일 시스템에 쓰기, durable storage로 비동기 복제
3. session stop — microVM 종료, 미 flush 데이터는 graceful shutdown 시 flush
4. 같은 session resume — 새 microVM에 storage 복원

본 프로젝트는 `/mnt/workspace/langgraph_checkpoints.sqlite`에 LangGraph checkpoint를 저장합니다. cold start 후 `ensure_checkpointer()` 로그가 `opened (existing)`이면 복원 성공, `initialized`이면 **새 DB 생성(이전 history 없음)** 입니다.

> **중요:** Dockerfile의 `ENV SESSION_STORAGE_DIR=/mnt/workspace`만으로는 영속 storage가 활성화되지 않습니다. **반드시** runtime API에 `filesystemConfigurations.sessionStorage`를 설정해야 합니다. `create_agent_runtime`뿐 아니라 **`update_agent_runtime`에도 동일하게 포함**해야 합니다. update 시 누락하면 `get-agent-runtime` 응답에 `filesystemConfigurations`가 없고, cold start마다 checkpoint가 사라집니다.

#### maxLifetime · idleRuntimeSessionTimeout (lifecycle)

[Lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)의 **8시간**은 checkpoint **데이터 보관 기간이 아닙니다.** microVM **인스턴스 최대 수명**입니다.

| 설정 | 기본값 | 의미 |
|------|--------|------|
| `idleRuntimeSessionTimeout` | 900초 (**15분**) | idle 상태가 이 시간 지속되면 해당 session의 microVM 종료 |
| `maxLifetime` | 28,800초 (**8시간**) | microVM이 한 번 생성된 뒤 살아 있을 수 있는 **최대 시간** (리셋 불가) |

- idle timeout 도달 → microVM만 종료. sessionStorage가 설정되어 있고 **같은 `runtimeSessionId`**로 다시 invoke하면 storage가 복원되어야 합니다.
- maxLifetime 도달 → microVM 교체. session 자체는 새 microVM으로 **resume 가능** (문서: *"The session itself can persist beyond this with a new instance provisioned."*)
- idle timer는 **같은 session에 invoke할 때마다 리셋**됩니다.

#### runtimeSessionId (클라이언트)

[application/agentcore_client.py](./application/agentcore_client.py)의 `runtime_session_id_for()`는 history 모드에서 user_id 기반 **고정 UUID**를 생성합니다. sessionStorage 복원은 **invoke마다 동일한 `runtimeSessionId`**가 전달될 때만 동작합니다.

- history 모드에서 `runtimeSessionId`는 `user_id`만으로 고정 (`agentcore_client.py`)

#### 배포·운영 체크리스트

1. `get-agent-runtime`으로 `filesystemConfigurations`에 `sessionStorage` 존재 확인
2. create/update 모두 `/mnt/workspace` mount path 포함
3. history 모드에서 `runtimeSessionId`가 user_id마다 고정인지 확인
4. runtime **version 업데이트 직후**에는 session data가 wipe됨 (정상 동작)
5. CloudWatch(`/aws/bedrock-agentcore/runtimes/...`)에서 `checkpointer` 로그로 `initialized` vs `opened (existing)` 확인

#### 참고 문서

- [File system configurations for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)
- [Configure lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)
- [AgentCore quotas (session storage limits)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)


### Message Trim

LangGraph 에이전트([runtime_agent/langgraph/langgraph_agent.py](./runtime_agent/langgraph/langgraph_agent.py)의 `call_model`)는 LLM 호출 직전에 **HumanMessage 기준 최근 N턴**만 남깁니다. LangGraph state의 `messages`는 checkpointer에 그대로 두고, **모델에 넘기는 메시지만** trim합니다. `history_mode=Enable`/`Disable` 모두 동일하게 적용됩니다.

**기본값:** `MAX_CONTEXT_TURNS = 5`

**설정 변경:**

- [runtime_agent/langgraph/langgraph_agent.py](./runtime_agent/langgraph/langgraph_agent.py)의 `MAX_CONTEXT_TURNS` 상수 수정
- 또는 [runtime_agent/langgraph/chat.py](./runtime_agent/langgraph/chat.py)의 `create_agent()`에서 config의 `max_turns` / `configurable.max_turns` 지정
- `max_turns=0`이면 trim 비활성화

상수와 trim 함수는 `langgraph_agent.py`에 정의합니다.

```python
# runtime_agent/langgraph/langgraph_agent.py
MAX_CONTEXT_TURNS = 5


def trim_messages_by_human_turns(messages: list, max_turns: int) -> list:
    """Keep messages from the last N HumanMessage turns (inclusive)."""
    if max_turns <= 0 or not messages:
        return messages

    human_indices = [i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage)]
    if len(human_indices) <= max_turns:
        return messages

    return messages[human_indices[-max_turns]:]
```

`call_model`에서는 Bedrock용 메시지 정규화(`sanitize_messages_for_bedrock`) 후 trim을 적용합니다.

```python
# runtime_agent/langgraph/langgraph_agent.py — call_model() 내부
        max_turns = (
            config.get("configurable", {}).get("max_turns")
            or config.get("max_turns")
            or MAX_CONTEXT_TURNS
        )
        trimmed = trim_messages_by_human_turns(messages, max_turns)
        if len(trimmed) < len(messages):
            logger.info(
                f"trimmed messages from {len(messages)} to {len(trimmed)} "
                f"(max_turns={max_turns})"
            )
            messages = trimmed

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        chain = prompt | model
        async for chunk in chain.astream({"messages": messages}):
            ...
```

에이전트 config는 `chat.py`의 `create_agent()`에서 생성하며, `history_mode`와 관계없이 `max_turns`를 전달합니다.

```python
# runtime_agent/langgraph/chat.py — create_agent()
    if history_mode == "Enable":
        app = langgraph_agent.buildChatAgentWithHistory(tools)
        config = {
            "recursion_limit": 100,
            "configurable": {
                "thread_id": thread_id,
                "tools": tools,
                "system_prompt": system_prompt,
            },
            "max_turns": langgraph_agent.MAX_CONTEXT_TURNS,
        }
    else:
        app = langgraph_agent.buildChatAgent(tools)
        config = {
            "recursion_limit": 100,
            "configurable": {
                "thread_id": thread_id,
                "tools": tools,
                "system_prompt": system_prompt,
            },
            "max_turns": langgraph_agent.MAX_CONTEXT_TURNS,
        }
```

**`max_turns=5`의 의미**

- **사용자 HumanMessage 5개**와, 각 턴에 이어진 **모든 후속 메시지**를 유지
- 1턴 = `HumanMessage` 1개 + 그 뒤의 `AIMessage`, `ToolMessage`, 도구 feedback loop 전체
- 도구를 여러 번 호출해도 **같은 사용자 질문이면 1턴**으로 카운트

**예 (도구 사용 포함)**

```
Human(Q1) → AI(tool_calls) → ToolMessage → AI(A1)
Human(Q2) → AI(A2)
Human(Q3) → AI(tool_calls) → ToolMessage → AI(A3)
```

`max_turns=2`이면 **Q2부터** 유지:

```
Human(Q2) → AI(A2) → Human(Q3) → AI(tool_calls) → ToolMessage → AI(A3)
```

**메시지 개수 trim과의 차이**

| 방식 | `N=5`일 때 |
|------|------------|
| 이전 (메시지 개수) | 메시지 객체 5개만 유지 → 도구 루프 때문에 사용자 턴 수가 불규칙 |
| 현재 (HumanMessage 턴) | 사용자 질문 5개 + 각 턴의 AI/Tool 응답 전체 유지 |

**Session Storage와의 관계**

- checkpointer(SQLite)에는 **전체 대화 이력**이 저장됩니다.
- trim은 LLM 컨텍스트 윈도우 관리용이며, 저장된 history를 삭제하지 않습니다.
- CloudWatch 로그에서 `trimmed messages from X to Y (max_turns=5)`로 trim 여부를 확인할 수 있습니다.

## 배포하기

아래와 같이 EC2를 이용해 배포 환경을 구성합니다.

1. AWS Console의 EC2에 접속해서 [Launch instance]를 선택합니다.
2. EC2 생성시 Architecture로 Arm64을 선택하고 나머지는 기본값으로 생성합니다.
3. [EC2 Instance Connect]로 접속해서 아래와 같이 python, pip, git, boto3를 설치합니다.

```text
sudo yum install python3 python3-pip git 
pip install boto3 
```

4. 아래 명령어로 docker를 설치합니다.

```bash
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker ec2-user
newgrp docker
docker info
```

5. Workshop의 경우에 아래 형태로 된 Credential을 복사하여 EC2 터미널에 입력합니다.

<img width="700" alt="credential" src="https://github.com/user-attachments/assets/261a24c4-8a02-46cb-892a-02fb4eec4551" />


6. 아래와 같이 git source를 가져옵니다.

```python
git clone https://github.com/kyopark2014/langgraph-runtime
```

7. 아래와 같이 [installer.py](./installer.py)를 이용해 설치를 시작합니다.

```text
python3 langgraph-runtime/installer.py
```

8. 설치가 완료되면 CloudFront로 접속하여 동작을 확인합니다. Agent를 선택한 후에 적절한 MCP tool을 선택하여 원하는 작업을 수행합니다.

9. 인프라가 더이상 필요없을 때에는 루트 [uninstaller.py](./uninstaller.py)를 이용해 제거합니다. AgentCore Runtime, S3 Files, VPC, ECS, Knowledge Base와 함께 `application/config.json`도 정리됩니다.

```text
python uninstaller.py
```

### Knowledge Base 문서 동기화 하기 

Knowledge Base에서 문서를 활용하기 위해서는 S3에 문서 등록 및 동기화기 필요합니다. [S3 Console](https://us-west-2.console.aws.amazon.com/s3/home?region=us-west-2)에 접속하여 "storage-for-agentcore-xxxxxxxxxxxx-us-west-2"를 선택하고, 아래와 같이 docs폴더를 생성한 후에 파일을 업로드 합니다. 

<img width="400" alt="image" src="https://github.com/user-attachments/assets/482f635e-a38d-4525-b9a3-fb1c2a9089c8" />

이후 [Knowledge Bases Console](https://us-west-2.console.aws.amazon.com/bedrock/home?region=us-west-2#/knowledge-bases)에 접속하여, "agentcore"라는 Knowledge Base를 선택합니다. 이후 아래와 같이 [Sync]를 선택합니다.

<img width="1533" height="287" alt="noname" src="https://github.com/user-attachments/assets/2edd3b6b-dbce-4784-b640-139fa84cc223" />

### Local에서 실행하기

AWS 환경을 잘 활용하기 위해서는 [AWS CLI를 설치](https://docs.aws.amazon.com/ko_kr/cli/v1/userguide/cli-chap-install.html)하여야 합니다. EC2에서 배포하는 경우에는 별도로 설치가 필요하지 않습니다. Local에 설치시는 아래 명령어를 참조합니다.

```text
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" 
unzip awscliv2.zip
sudo ./aws/install
```

AWS credential을 아래와 같이 AWS CLI를 이용해 등록합니다.

```text
aws configure
```

설치하다가 발생하는 각종 문제는 [Kiro-cli](https://aws.amazon.com/ko/blogs/korea/kiro-general-availability/)를 이용해 빠르게 수정합니다. 아래와 같이 설치할 수 있지만, Windows에서는 [Kiro 설치](https://kiro.dev/downloads/)에서 다운로드 설치합니다. 실행시는 셀에서 "kiro-cli"라고 입력합니다. 

```python
curl -fsSL https://cli.kiro.dev/install | bash
```

venv로 환경을 구성하면 편리하게 패키지를 관리합니다. 아래와 같이 환경을 설정합니다.

```text
python -m venv .venv
source .venv/bin/activate
```

이후 다운로드 받은 github 폴더로 이동한 후에 아래와 같이 필요한 패키지를 추가로 설치 합니다.

```text
pip install -r requirements.txt
```

이후 아래와 같은 명령어로 Web UI를 실행합니다. UI 변경 후에는 `application/web`에서 `npm run build`를 먼저 실행해야 합니다. ([App UI](#app-ui) 참조)

```text
cd application/web && npm install && npm run build
cd ../..
uvicorn application.server:app --host 0.0.0.0 --port 8501
```



### 비동기 실행

에이전트가 즉시 응답하고 백그라운드에서 계속 처리할 수 있습니다. 클라이언트는 동기/비동기 구분 없이 동일한 API 사용가능하고, 세션을 재사용하여 컨텍스트 유지합니다.

```python
import threading
import time
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@tool
def start_background_task(duration: int = 5) -> str:
    """백그라운드에서 지정된 시간 동안 실행되는 태스크 시작"""

    # 비동기 태스크 등록
    task_id = app.add_async_task("background_processing", {"duration": duration})

    # 별도 스레드에서 백그라운드 작업 실행
    def background_work():
        time.sleep(duration)  # 실제 작업 수행
        app.complete_async_task(task_id)  

    threading.Thread(target=background_work, daemon=True).start()

    return f"백그라운드 태스크 시작됨 (ID: {task_id}), {duration}초 후 완료 예정"

agent = Agent(tools=[start_background_task])

@app.entrypoint
def main(payload):
    user_message = payload.get("prompt", "3초짜리 태스크를 시작해줘")
    return {"message": agent(user_message).message}

if __name__ == "__main__":
    app.run()
```

## Guardrail

`installer.py`가 Amazon Bedrock Guardrail을 자동으로 생성·업데이트합니다. 사용자 입력에서 **성적 표현**과 **프롬프트 공격**(jailbreak, prompt injection)을 차단합니다.

### 설치 시 동작

`python installer.py` 실행 시 아래 순서로 Guardrail이 처리됩니다.

1. IAM 정책·역할 생성
2. **Bedrock Guardrail 생성/업데이트** (`create_bedrock_guardrail`)
3. Docker 이미지 빌드 및 ECR 푸시
4. AgentCore Runtime 생성/업데이트

동일 이름의 Guardrail이 이미 있으면 `update_guardrail`로 정책을 갱신하고, 없으면 `create_guardrail`로 새로 만듭니다.

### 콘텐츠 필터 정책

| 필터 | 입력 | 출력 | 동작 |
|------|------|------|------|
| `SEXUAL` | HIGH | HIGH | 성적 표현이 포함된 질문·응답 차단 |
| `PROMPT_ATTACK` | HIGH | NONE | jailbreak·프롬프트 인젝션 차단 (입력 전용) |

`PROMPT_ATTACK`은 입력에만 적용되므로 `outputStrength`는 AWS API 요구사항에 따라 `NONE`으로 설정합니다.

### 차단 메시지

- **입력 차단**: `요청이 안전 정책에 의해 차단되었습니다. 성적 표현 또는 프롬프트 공격이 감지되었습니다.`
- **출력 차단**: `응답이 안전 정책에 의해 차단되었습니다.`

### config.json 저장 항목

설치 완료 후 `config.json`에 아래 값이 저장됩니다.

| 키 | 설명 |
|----|------|
| `guardrail_id` | Guardrail ID |
| `guardrail_version` | Guardrail 버전 (`DRAFT`) |
| `guardrail_arn` | Guardrail ARN |
| `guardrail_name` | `guardrail-for-{projectName}` 형식의 이름 |

### IAM 권한

AgentCore Runtime 역할(`AmazonBedrockAgentCoreRuntimeRoleFor{projectName}`)에 아래 권한이 추가됩니다.

- `bedrock:GetGuardrail`
- `bedrock:ListGuardrails`
- `bedrock:ApplyGuardrail`

리소스 범위: `arn:aws:bedrock:{region}:{accountId}:guardrail/*`

### Guardrail 생성 예시

`installer.py` 내부에서 아래와 같이 Guardrail을 구성합니다.

```python
bedrock_client = boto3.client("bedrock", region_name=region)

response = bedrock_client.create_guardrail(
    name=f"guardrail-for-{project_name}",
    description="Content safety guardrail: blocks sexual content and prompt attacks.",
    contentPolicyConfig={
        "filtersConfig": [
            {
                "type": "SEXUAL",
                "inputStrength": "HIGH",
                "outputStrength": "HIGH",
                "inputAction": "BLOCK",
                "outputAction": "BLOCK",
                "inputModalities": ["TEXT"],
                "outputModalities": ["TEXT"],
            },
            {
                "type": "PROMPT_ATTACK",
                "inputStrength": "HIGH",
                "outputStrength": "NONE",
                "inputAction": "BLOCK",
                "outputAction": "NONE",
                "inputModalities": ["TEXT"],
            },
        ]
    },
    blockedInputMessaging="요청이 안전 정책에 의해 차단되었습니다. ...",
    blockedOutputsMessaging="응답이 안전 정책에 의해 차단되었습니다.",
)
```

### 추론 시 Guardrail 적용

Guardrail 리소스 생성만으로는 모델 호출 시 자동 적용되지 않습니다. Web UI 사이드바 Settings의 **Guardrail** 토글로 on/off를 제어하고, `guardrail_enabled` 값이 AgentCore payload로 Runtime에 전달됩니다.

모델 종류에 따라 적용 방식이 나뉩니다.

| 모델 | 적용 방식 | 설명 |
|------|-----------|------|
| Claude / Nova | `ChatBedrockConverse` + `guardrail_config` | 입력·출력 모두 Converse API Guardrail로 검사 |
| OpenAI 등 | `check_input_guardrail()` + `apply_guardrail` | 모델 호출 전 입력만 사전 검사 |

#### Claude / Nova: Converse API Guardrail

`get_chat()`에서 Guardrail이 활성화되고 모델 타입이 Claude 또는 Nova이면, 기존 `ChatBedrock` 대신 `ChatBedrockConverse`를 생성합니다. `_guardrail_config()`가 반환한 `guardrail_config`를 생성자에 넘겨 Converse API 호출 시 입력·출력 모두 Guardrail 검사가 적용됩니다.

```python
guardrail_cfg = _guardrail_config()
if guardrail_cfg and profile["model_type"] in ("claude", "nova"):
    boto3_bedrock = boto3.client(
        service_name="bedrock-runtime",
        region_name=bedrock_region,
        config=Config(
            retries={"max_attempts": 30},
            read_timeout=300,
        ),
    )
    converse_kwargs = {
        "model_id": modelId,
        "client": boto3_bedrock,
        "max_tokens": maxOutputTokens,
        "temperature": 0.1,
        "region_name": bedrock_region,
        "guardrail_config": guardrail_cfg,
    }
    if model_type == "claude":
        converse_kwargs["provider"] = "anthropic"
    converse_chat = ChatBedrockConverse(**converse_kwargs)
    converse_chat.streaming = False
    return converse_chat
```

`_guardrail_config()`는 `config.json`의 Guardrail ID·버전을 아래 형태로 조합합니다.

```python
guardrail_config = {
    "guardrailIdentifier": config["guardrail_id"],
    "guardrailVersion": config.get("guardrail_version", "DRAFT"),
    "trace": "enabled",
}
```

동작 요약:

1. `guardrail_enabled`가 `True`이고 `guardrail_id`가 `config.json`에 있을 때만 `guardrail_cfg`가 생성됩니다.
2. Claude 모델은 `provider="anthropic"`을 지정합니다.
3. `ChatBedrockConverse`에 `guardrail_config`를 전달하면 모델 추론 요청마다 입력·출력이 Guardrail로 검사됩니다.
4. Guardrail이 비활성화되었거나 Claude/Nova가 아니면 아래 `ChatBedrock` 경로로 폴백합니다.

#### OpenAI 등: 입력 사전 검사 (`apply_guardrail`)

`ChatBedrockConverse`를 쓰지 않는 모델(OpenAI 등)은 `agent.py`에서 에이전트 실행 전 `chat.check_input_guardrail()`을 호출합니다. 내부적으로 Bedrock Runtime의 `apply_guardrail` API로 사용자 질문을 검사하고, 차단되면 모델 호출 없이 안내 메시지를 반환합니다.

```python
client = boto3.client("bedrock-runtime", region_name=bedrock_region)
response = client.apply_guardrail(
    guardrailIdentifier=guardrail_cfg["guardrailIdentifier"],
    guardrailVersion=guardrail_cfg["guardrailVersion"],
    source="INPUT",
    content=[{"text": {"text": text}}],
)
if response.get("action") == "GUARDRAIL_INTERVENED":
    logger.info("Guardrail blocked user input")
    for output in response.get("outputs", []):
        text_output = output.get("text", {})
        if text_output.get("text"):
            return True, text_output["text"]
    return (
        True,
        "요청이 안전 정책에 의해 차단되었습니다. "
        "성적 표현 또는 프롬프트 공격이 감지되었습니다.",
    )
```

동작 요약:

1. `source="INPUT"`으로 사용자 질문만 검사합니다.
2. `action`이 `GUARDRAIL_INTERVENED`이면 Guardrail이 입력을 차단한 것입니다.
3. `outputs`에 Guardrail이 정의한 차단 메시지가 있으면 그대로 사용자에게 반환합니다.
4. 차단 메시지가 없으면 기본 한국어 안내 문구를 반환합니다.

`agent.py` 호출 흐름:

```python
if query and chat.guardrail_enabled and not chat.uses_converse_guardrail():
    blocked, blocked_message = chat.check_input_guardrail(query)
    if blocked:
        yield {"result": {"messages": [{"role": "assistant", "content": blocked_message}], "image_url": []}}
        return
```

Claude/Nova는 `uses_converse_guardrail()`이 `True`이므로 위 사전 검사는 건너뛰고, Converse API Guardrail이 입력·출력을 함께 처리합니다.

Guardrail 동작시 결과는 아래와 같습니다. 

<img width="718" height="215" alt="image" src="https://github.com/user-attachments/assets/b815edfb-9617-4799-8f27-39d45c408068" />

## Observability Setup

AgentCore Evaluations는 CloudWatch에 수집된 OpenTelemetry span을 읽어 품질을 점수화합니다. 따라서 **Observability(트레이스 수집)가 Evaluation의 전제 조건**입니다.

### 자동 설정 (installer)

[runtime_agent/langgraph/installer.py](./runtime_agent/langgraph/installer.py) 설치 시 `setup_agentcore_observability()` 단계에서 아래를 자동 구성합니다.

| 항목 | 모듈 | 설명 |
|------|------|------|
| CloudWatch Transaction Search | [observability.py](./runtime_agent/langgraph/observability.py) | `aws/spans` 로그 그룹, X-Ray trace destination |
| Runtime trace delivery | `observability.py` | AgentCore Runtime → CloudWatch TRACES 전달 |
| Telemetry evaluation | `observability.py` | CloudWatch Observability Admin 평가 시작 |

```bash
cd runtime_agent/langgraph
python3 installer.py
```

설치 후 `config.json`에 `agent_runtime_arn`이 저장되며, GenAI Observability 콘솔에서 trace·span을 확인할 수 있습니다.

### Runtime 컨테이너 계측

[runtime_agent/langgraph/Dockerfile](./runtime_agent/langgraph/Dockerfile)과 [agent.py](./runtime_agent/langgraph/agent.py)에 아래가 포함되어 있습니다.

| 구성 요소 | 역할 |
|-----------|------|
| `aws-opentelemetry-distro` | ADOT — CloudWatch로 span 전송 |
| `opentelemetry-instrumentation-langchain` | LangGraph/LangChain 호환 span 생성 (Evaluation 필수 scope) |
| `opentelemetry-instrument` (CMD) | uvicorn 프로세스 자동 계측 |
| `LangchainInstrumentor().instrument()` | `opentelemetry.instrumentation.langchain` scope로 trace 발행 |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` | LLM 입·출력 내용을 span에 포함 (평가에 필요) |
| `OTEL_RESOURCE_ATTRIBUTES=service.name=runtime_langgraph.DEFAULT` | Evaluation 데이터 소스의 service name |

Evaluation이 인식하는 span scope:

- `opentelemetry.instrumentation.langchain`
- `openinference.instrumentation.langchain`

ADOT만으로는 `starlette`, `httpx` span만 생성되므로 **LangChain instrumentation이 반드시 필요**합니다.

### 수동 확인

1. Agent를 1~2회 호출한 뒤 **2~5분** 대기 (span 수집 지연)
2. CloudWatch 로그 그룹 확인:
   - `/aws/bedrock-agentcore/runtimes/runtime_langgraph-<id>-DEFAULT`
   - `aws/spans` (Transaction Search)
3. [GenAI Observability 콘솔](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)에서 trace 확인

> Transaction Search가 계정에서 한 번도 활성화되지 않았다면 span export가 최대 10~15분 지연될 수 있습니다.

## AgentCore Evaluations

[Amazon Bedrock AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)는 CloudWatch에 수집된 OpenTelemetry span을 LLM-as-a-Judge로 점수화합니다. LangGraph는 `opentelemetry-instrumentation-langchain`(또는 OpenInference)으로 계측해야 Evaluation이 인식하는 scope를 만듭니다.

전제 조건은 [Observability Setup](#observability-setup)입니다. installer가 Observability → Evaluations 순으로 설정합니다.

```bash
cd runtime_agent/langgraph
python3 installer.py
```

### 1. Online Evaluation 설정

Observability 다음 단계로 [evaluation.py](./runtime_agent/langgraph/evaluation.py)의 `setup_agentcore_evaluations()`가 실행됩니다.

| 항목 | 값 |
|------|-----|
| IAM 역할 | `AmazonBedrockAgentCoreEvaluationRoleFor{projectName}` |
| Config 이름 | `{projectName}_langgraph_online_eval` (예: `langgraph_runtime_langgraph_online_eval`) |
| Evaluator | `Builtin.Helpfulness`, `Builtin.GoalSuccessRate`, `Builtin.ToolSelectionAccuracy` |
| Sampling | 10% |
| `sessionTimeoutMinutes` | **5분** |
| Data source | log group `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, service `runtime_langgraph.DEFAULT` |
| 결과 로그 | `/aws/bedrock-agentcore/evaluations/results/<config-id>` |

`config.json`에 저장되는 키: `evaluation_execution_role_arn`, `online_evaluation_config_name`, `evaluation_service_name`, `evaluation_log_group`, `evaluation_session_timeout_minutes`.

콘솔: **Amazon Bedrock AgentCore → Evaluation**.

#### `sessionTimeoutMinutes`

Online evaluation은 같은 `session.id`(대개 AgentCore `runtimeSessionId`)의 span을 모은 뒤, **마지막 활동 이후 N분 유휴**하면 세션이 끝난 것으로 보고 평가합니다.

- 기본(서비스): 15분 → 이 프로젝트는 **5분**으로 설정
- Chat 모드(`history_mode=Enable`)는 user별 `runtimeSessionId`가 고정이라 턴이 한 세션에 계속 쌓임
- timeout이 길면 세션 span이 [한도](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)(**1000 spans / 15 MB**)를 넘어 `ValidationException`이 납니다

에이전트 대화 세션을 끊는 설정이 아니라, **평가용 세션 경계를 나누는 타이머**입니다. 값은 `evaluation.py`의 `DEFAULT_SESSION_TIMEOUT_MINUTES`에서 바꾸며, installer 재실행 시 기존 config를 `update_online_evaluation_config`로 갱신합니다.

### 2. On-demand 평가 (개발·검증)

에이전트 호출 후 **특정 세션의 span을 직접 넣어** 즉시 평가합니다. Online evaluation과 달리 sampling/idle timeout을 기다리지 않습니다.

> Data-plane `Evaluate` API는 `sessionId`만 받는 API가 **아닙니다**. CloudWatch(`aws/spans`)에서 조회한 OTEL span JSON을 `evaluationInput.sessionSpans`로 전달해야 합니다 (세션당 **최대 1000 spans / 15 MB**).

**AgentCore CLI** (프로젝트/`agentcore` 환경에 따라 span 수집을 대행할 수 있음):

```bash
agentcore run eval \
  --runtime runtime_langgraph \
  --session-id "<runtimeSessionId>" \
  --evaluator Builtin.Helpfulness Builtin.GoalSuccessRate
```

**boto3** (span을 이미 수집한 경우):

```python
import boto3

client = boto3.client("bedrock-agentcore", region_name="us-west-2")
# session_spans: aws/spans에서 해당 session.id의 OTEL span 객체 리스트
response = client.evaluate(
    evaluatorId="Builtin.Helpfulness",
    evaluationInput={"sessionSpans": session_spans},
    # 선택: 특정 trace만 평가
    # evaluationTarget={"traceIds": ["<traceId>"]},
)
```

개발 중 품질 게이트·단일 세션 재현에 적합합니다. 장시간 Chat 세션은 span 한도를 넘기기 쉬우므로 **짧은 세션** 또는 `evaluationTarget.traceIds`로 범위를 줄여 호출하세요.

### 3. Online 평가 (운영 모니터링)

installer가 만든 online evaluation config가 `enableOnCreate=True`로 활성화되면, 샘플링된 운영 세션이 **자동으로** 평가됩니다. 결과는 `/aws/bedrock-agentcore/evaluations/results/<config-id>`에 JSON으로 저장됩니다.

콘솔: **Amazon Bedrock AgentCore → Evaluation**

운영 트래픽 모니터링용입니다. 이미 config가 있으면 installer/`evaluation.py`가 `update_online_evaluation_config`로 rule(sampling, `sessionTimeoutMinutes`)을 갱신합니다.

수동 생성 예:

```python
import boto3

client = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
client.create_online_evaluation_config(
    onlineEvaluationConfigName="langgraph_runtime_langgraph_online_eval",
    rule={
        "samplingConfig": {"samplingPercentage": 10.0},
        "sessionConfig": {"sessionTimeoutMinutes": 5},
    },
    dataSourceConfig={
        "cloudWatchLogs": {
            "logGroupNames": [
                "/aws/bedrock-agentcore/runtimes/runtime_langgraph-<id>-DEFAULT"
            ],
            "serviceNames": ["runtime_langgraph.DEFAULT"],
        }
    },
    evaluators=[
        {"evaluatorId": "Builtin.Helpfulness"},
        {"evaluatorId": "Builtin.GoalSuccessRate"},
        {"evaluatorId": "Builtin.ToolSelectionAccuracy"},
    ],
    evaluationExecutionRoleArn=(
        "arn:aws:iam::<account>:role/"
        "AmazonBedrockAgentCoreEvaluationRoleFor<projectName>"
    ),
    enableOnCreate=True,
)
```

| 구분 | On-demand | Online |
|------|-----------|--------|
| 용도 | 개발·재현·CI | 운영 연속 모니터링 |
| 트리거 | API/CLI로 즉시 | sampling + session idle 후 자동 |
| 입력 | `sessionSpans` 직접 전달 | CloudWatch log group + service name |
| 이 프로젝트 | 수동 호출 | installer가 config 생성/갱신 |

### 4. Built-in Evaluator

| Evaluator | 레벨 | 용도 |
|-----------|------|------|
| `Builtin.Helpfulness` | Trace | 응답 유용성 |
| `Builtin.GoalSuccessRate` | Session | 목표 달성 |
| `Builtin.ToolSelectionAccuracy` | Tool call | 도구 선택 정확도 |
| `Builtin.Correctness` | Trace | 사실 정확성 (ground truth 필요) |
| `Builtin.InstructionFollowing` | Trace | 지시 준수 |
| `Builtin.TrajectoryExactOrderMatch` | Session | 도구 호출 순서 검증 |

### 5. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| Dashboard/결과가 비어 있음 | 샘플링(10%) 미포함, 또는 session idle(5분) 전 | 여러 세션 호출 후 5분+ 대기, 결과 로그 그룹 확인 |
| `Session cannot be evaluated as the size of all spans... exceeds the maximum limit` | Chat 장시간 세션 + 메시지 content capture로 span이 1000개/15MB 초과 | `sessionTimeoutMinutes` 유지(5분), 짧은 세션으로 검증, 필요 시 content capture 축소 |
| `no spans with supported scope` | LangChain instrumentation 미적용 | Dockerfile / `LangchainInstrumentor` 확인 후 이미지 재배포 |
| span은 있으나 평가 없음 | Transaction Search 미활성 또는 수집 지연 | installer Observability 단계 재실행, 2~5분 대기 |
| 메시지 내용이 평가에 없음 | GenAI content capture 비활성 | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` |

### 6. 적용 순서

1. `python3 installer.py` — 이미지 배포 + Observability + Evaluations
2. Agent 호출 → `aws/spans`에서 LangChain scope span 확인
3. On-demand로 단일 세션 검증 (선택) → Online evaluation이 운영 트래픽 모니터링
4. 5분 유휴 후 Evaluation 콘솔 / `/aws/bedrock-agentcore/evaluations/results/...` 에서 점수 확인

## Dashboard

LangGraph AgentCore Runtime의 운영 상태·토큰 사용량·예상 비용을 CloudWatch 대시보드에서 확인할 수 있습니다. [runtime_agent/langgraph/installer.py](./runtime_agent/langgraph/installer.py) 설치 마지막 단계에서 대시보드가 자동 생성되며, 이름은 `{projectName}-monitoring` 형식입니다.

### 생성 방법

루트 인프라 배포 후 LangGraph Runtime installer를 실행하면 대시보드가 함께 생성됩니다.

```bash
cd runtime_agent/langgraph
python3 installer.py
```

설치가 완료되면 터미널에 CloudWatch 대시보드 URL이 출력됩니다. `config.json`의 `cloudwatch_dashboard_name`에도 대시보드 이름이 저장됩니다.

```
https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#dashboards/dashboard/{projectName}-monitoring
```

대시보드만 다시 만들려면 installer를 재실행하거나, `installer.py`의 `create_monitoring_dashboard()`를 호출합니다.

### 구성 요소

| 구분 | 메트릭 소스 | 주요 항목 |
|------|-------------|-----------|
| Runtime 운영 | `AWS/Bedrock-AgentCore` (AgentCore vended) | Invocations, Session Count, Latency, Errors, Throttles |
| 리소스 사용 | `AWS/Bedrock-AgentCore` | CPUUsed-vCPUHours, MemoryUsed-GBHours |
| 토큰·모델 비용 | `LangGraph/AgentCoreRuntime` (커스텀) | InputTokens, OutputTokens, TotalTokens, EstimatedModelCostUSD, LLMInvocations |

**커스텀 토큰 메트릭**은 [runtime_agent/langgraph/langgraph_agent.py](./runtime_agent/langgraph/langgraph_agent.py)의 `call_model`에서 LLM 응답의 `usage_metadata`를 읽어 [runtime_agent/langgraph/cloudwatch_metrics.py](./runtime_agent/langgraph/cloudwatch_metrics.py)가 CloudWatch에 발행합니다. 대시보드 정의와 비용 추정 로직도 동일 모듈에 있습니다.

### 대시보드 위젯

- **Runtime**: 호출 수, 세션 수, 지연 시간(p99), 시스템/사용자 오류, 스로틀
- **토큰**: Input/Output/Total Tokens, 모델별 Total Tokens, LLM 호출 수
- **리소스**: Runtime CPU(vCPU-Hours), Memory(GB-Hours)
- **예상 비용(USD)**: 모델 비용, Runtime CPU 비용, Runtime 메모리 비용, **총 예상 비용**(모델 + CPU + 메모리)
- **24시간 요약**: Total Tokens, Model Cost, Invocations, Total Cost

### 비용 추정 기준

대시보드의 비용은 **추정치**이며, 실제 청구액은 AWS 청구서를 기준으로 합니다.

| 항목 | 단가 (USD) |
|------|------------|
| Runtime CPU | $0.0895 / vCPU-hour |
| Runtime Memory | $0.00945 / GB-hour |
| 모델 토큰 | Bedrock on-demand 단가 (예: Claude Sonnet $3 / $15 per 1M input/output tokens) |

모델별 단가는 `cloudwatch_metrics.py`의 `MODEL_PRICING_PER_MILLION`에 정의되어 있으며, 등록되지 않은 모델은 기본값(입력 $3, 출력 $15 / 1M tokens)으로 추정합니다.

### IAM 및 주의사항

- AgentCore Runtime IAM 역할에 `cloudwatch:PutMetricData` 권한이 포함되어야 토큰 메트릭이 발행됩니다. installer가 `AmazonBedrockAgentCoreRuntimePolicyFor{projectName}` 정책을 갱신합니다.
- **토큰 메트릭**은 `cloudwatch_metrics.py`가 포함된 Docker 이미지를 배포한 뒤 LLM 호출부터 수집됩니다. 대시보드만 재생성한 경우에도 Runtime 이미지를 다시 빌드·배포해야 토큰 차트에 데이터가 표시됩니다.
- AgentCore vended 메트릭(`CPUUsed-vCPUHours`, `MemoryUsed-GBHours` 등)은 최대 **60분** 지연될 수 있습니다.
- GenAI Observability 콘솔에서 trace·span을 함께 보려면 [CloudWatch Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)를 계정에서 한 번 활성화해야 합니다.

생성된 Dashboard는 아래와 같습니다.

<img width="1000" alt="image" src="https://github.com/user-attachments/assets/88390975-6131-4c6a-9f17-5f01bc3400f4" />


## 실행 결과

"https://github.com/kyopark2014/strands-runtime/blob/main/README.md 을 정리해줘."와 같이 입력하면 웹의 정보를 편리하게 활용할 수 있습니다.

<img width="728" height="729" alt="image" src="https://github.com/user-attachments/assets/c3a18138-ba1c-4956-90b4-d55a0737da33" />

이때의 결과는 아래와 같습니다.

<img width="663" height="780" alt="image" src="https://github.com/user-attachments/assets/6b4ed348-c923-46d7-838b-da8f54e123f8" />


"aws document로 agent evalutation 에 대해 조사해줘."로 하면 필요한 정보를 조회하여 정리합니다.

<img width="720" height="706" alt="image" src="https://github.com/user-attachments/assets/fb5eb40e-720e-420f-ad3b-8aafceab236e" />



## Reference 

[Invoke streaming agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)

[Get started with the Amazon Bedrock AgentCore Runtime starter toolkit](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-getting-started-toolkit.html)

[Amazon Bedrock AgentCore - Developer Guide](https://docs.aws.amazon.com/pdfs/bedrock-agentcore/latest/devguide/bedrock-agentcore-dg.pdf)

[BedrockAgentCoreControlPlaneFrontingLayer](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-agentcore-control.html)

[get_agent_runtime](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-agentcore-control/client/get_agent_runtime.html)

[Amazon Bedrock AgentCore Samples](https://github.com/awslabs/amazon-bedrock-agentcore-samples)

[Amazon Bedrock AgentCore](https://buttoned-gull-5fa.notion.site/Amazon-Bedrock-AgentCore-23708996fdd380c2a6e1ffaa2e08c000)

[Amazon Bedrock AgentCore RuntCode Interpreter](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/05-AgentCore-tools/01-Agent-Core-code-interpreter)

[Add observability to your Amazon Bedrock AgentCore resources](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)

[AgentCore generated runtime observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)

[Evaluate agent performance with Amazon Bedrock AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)

[Create online evaluation - Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-online-evaluations.html)

[AgentCore Evaluations prerequisites](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations-prerequisites.html)

[Hosting Strands Agents with Amazon Bedrock models in Amazon Bedrock AgentCore Runtime](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials%2F06-AgentCore-observability%2F01-Agentcore-runtime-hosted%2Fruntime_with_strands_and_bedrock_models.ipynb)

[Agentic AI 펀드 매니저](https://github.com/ksgsslee/investment_advisor_strands)

[AWS re:Invent 2025 - Architecting scalable and secure agentic AI with Bedrock AgentCore (AIM431)](https://www.youtube.com/watch?v=wqmeZOT6mmc)


[Deploy Production-Ready Agents in 22 Minutes with AgentCore Runtime](https://www.youtube.com/watch?v=Q-tYIAuv9WI)

[AgentCore Workshop](https://atomoh.gitbook.io/aiops)

