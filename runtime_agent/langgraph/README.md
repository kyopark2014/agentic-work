# 구현 내용

## Server

LangGraph Workflow를 아래와 같이 구현합니다. 아래는 기본적인 ReAct를 구현한 LangGraph workflow 입니다.

```python
from langgraph.prebuilt import ToolNode
from langgraph.graph import START, END, StateGraph

def buildChatAgent(tools):
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

    return workflow.compile() 

    return workflow.compile() 
```

Dockerfile을 아래와 같이 생성합니다.

```text
FROM --platform=linux/arm64 python:3.13-slim

WORKDIR /app

RUN pip install boto3 botocore --upgrade
RUN pip install langchain_aws langchain langchain_community langchain_experimental langgraph
RUN pip install mcp langchain-mcp-adapters
RUN pip install bedrock-agentcore bedrock-agentcore-starter-toolkit uv

# OpenTelemetry
RUN pip install aws-opentelemetry-distro>=0.10.0

COPY . .

# Add the current directory to Python path
ENV PYTHONPATH=/app

EXPOSE 8080

CMD ["uv", "run", "opentelemetry-instrument", "uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]
```


AgentCore에서 사용할 Agent를 agent.py로 구현합니다.

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@app.entrypoint
async def agent_langgraph(payload):
    query = payload.get("prompt")
    mcp_servers = payload.get("mcp_servers", [])
    user_id = payload.get("user_id")

    mcp_json = mcp_config.load_selected_config(mcp_servers)
    server_params = langgraph_agent.load_multiple_mcp_server_parameters(mcp_json)

    client = MultiServerMCPClient(server_params)
    tools = await client.get_tools()
    
    tool_list = [tool.name for tool in tools]
    
    app = langgraph_agent.buildChatAgent(tools)
    config = {
        "recursion_limit": 50,
        "configurable": {"thread_id": user_id},
        "tools": tools,
        "system_prompt": None
    }
    
    inputs = {
        "messages": [HumanMessage(content=query)]
    }
            
    value = final_output = None
    async for output in app.astream(inputs, config):
        for key, value in output.items():
            if key == "messages" or key == "agent":
                if isinstance(value, dict) and "messages" in value:
                    final_output = value
                elif isinstance(value, list):
                    final_output = {"messages": value, "image_url": []}
                else:
                    final_output = {"messages": [value], "image_url": []}

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
    
    yield({'result': final_output})

if __name__ == "__main__":
    app.run()
```

Agent를 배포합니다.

```python
client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
response = client.create_agent_runtime(
    agentRuntimeName=runtime_name,
    agentRuntimeArtifact={
        'containerConfiguration': {
            'containerUri': f"{accountId}.dkr.ecr.{aws_region}.amazonaws.com/{repositoryName}:{imageTags}"
        }
    },
    networkConfiguration={"networkMode":"PUBLIC"}, 
    roleArn=agent_runtime_role
)
print(f"response of create agent runtime: {response}")

agentRuntimeArn = response['agentRuntimeArn']
```


## Client

아래와 같이 runtime id와 session_id를 이용해 client에서 서버로 요청을 보내고 결과를 stream으로 수신합니다.

```python
prompt = "보일러 에러 코드?"
mcp_servers = ["kb-retriever"]
user_id = "user01"
runtime_session_id = str(uuid.uuid4())

payload = json.dumps({
    "prompt": prompt,
    "mcp_servers": mcp_servers,
    "user_id": user_id
})

agent_core_client = boto3.client('bedrock-agentcore', region_name=bedrock_region)
response = agent_core_client.invoke_agent_runtime(
    agentRuntimeArn=agent_runtime_arn,
    runtimeSessionId=runtime_session_id,
    payload=payload,
    qualifier="DEFAULT" # DEFAULT or LATEST
)

print(f"\n=== show stream response ===")
if "text/event-stream" in response.get("contentType", ""):
    for line in response["response"].iter_lines(chunk_size=10):
        line = line.decode("utf-8")
        if line:
            print(f"-> {line}")
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

Guardrail 리소스 생성만으로는 모델 호출 시 자동 적용되지 않습니다. Streamlit UI(`application/app.py`)의 **Guardrail 사용** 토글로 on/off를 제어하고, `guardrail_enabled` 값이 AgentCore payload로 Runtime에 전달됩니다.

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
