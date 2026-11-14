# Notion Remote MCP (채팅 내 Notion UI)

agentic-work는 Notion의 호스팅 MCP(`https://mcp.notion.com/mcp`)를 사용자별 OAuth로 연결하고, 채팅 안에 Notion 뷰어(최근 페이지 카드, 검색, 페이지 보기)를 렌더합니다. 기존 `notion`(stdio, 통합 토큰)은 그대로 두며 새 항목 이름은 **`notion-remote`** 입니다.

## 사용 방법

1. 웹 UI 로그인 후 Sidebar → **MCP** → `notion-remote` 체크
2. 처음이면 Notion 승인 팝업이 뜹니다. MCP 드로어의 **Notion 연결**로도 가능합니다.
3. 채팅에서 "최근 Notion 페이지 보여줘", "Notion에서 ○○ 검색" 등을 요청하면 뷰어가 렌더됩니다.
4. 설정 변경 후에는 **새 task**를 권장합니다. (에이전트는 토큰/MCP 선택이 바뀌면 다시 만들어집니다.)

연결 후 access token은 약 8시간이고 refresh token으로 자동 갱신됩니다(Drive처럼 자주 다시 연결할 필요 없음).

## 동작 흐름

```
Web UI  → GET /api/session/notion/authorize   (DCR + PKCE, 서버가 URL 생성)
        → Notion 승인 팝업 → GET /api/session/notion/callback (state로 사용자 식별, 토큰 저장)
chat invoke → runtime_mode: notion_auth.get_access_token (만료 임박 시 refresh)
            → AgentCore payload.notion_access_token
            → Runtime chat.create_agent → MCPAdapter → https://mcp.notion.com/mcp
              Authorization: Bearer <token>   (notion_bearer_auth.NotionBearerAuth)
도구 결과 → agent.py가 mcpApp(뷰어 HTML) 첨부 → McpAppFrame(iframe)
iframe 버튼(검색/열기) → POST /api/mcp-apps/call-tool → ECS가 사용자 토큰으로 Notion MCP 직접 호출
```

## 주요 파일

| 파일 | 역할 |
|------|------|
| `application/notion_auth.py`, `mcp_oauth.py` | Notion 공급자 설정 + 공용 OAuth(DCR/PKCE/토큰 저장·refresh, `{SESSION_STORAGE_DIR}/{user}/notion_oauth.json`). Canva와 `mcp_oauth.py`를 공유 |
| `application/api/routes_auth.py` | `/api/session/notion`, `/authorize`, `/callback` |
| `application/runtime_mode.py`, `agentcore_client.py` | invoke payload로 토큰 전달 |
| `application/mcp_apps_host.py` | iframe `tools/call` 프록시 (`notion-remote`, **읽기 전용 허용 목록**) |
| `runtime_agent/langgraph/mcp_config.py` | `notion-remote` MCP 정의 |
| `runtime_agent/langgraph/notion_bearer_auth.py` | 요청마다 Bearer 주입 |
| `runtime_agent/langgraph/notion_app.py` | 뷰어 HTML + 도구 등록 |
| `application/web/src/notionAuth.ts`, `components/Sidebar.tsx` | 연결 팝업 / 버튼 |

## 왜 Notion 공식 UI(`ui://notion/page`)를 쓰지 않는가

Notion MCP는 자체 MCP App을 제공하지만 (1) 어떤 도구의 `_meta.ui`에도 연결돼 있지 않고, (2) `app.notion.com`을 iframe으로 임베드하는데 `frame-ancestors`가 Notion/`claude.ai`/Claude 샌드박스 도메인만 허용하며, (3) 임베드 인증용 도구(`notion-*-embed-session`)가 일반 클라이언트에는 노출되지 않습니다. 그래서 도구 결과(JSON)를 직접 렌더하는 자체 뷰어를 씁니다. 페이지 편집은 "Notion에서 열기" 링크로 대신합니다.

## 주의

- **콜백 URL**: 로컬은 요청 Host, 그 외에는 `config.json`의 `sharing_url`(CloudFront 도메인)을 사용합니다. Notion에는 동적 등록(DCR)으로 자동 등록됩니다.
- **iss 끝 슬래시**: Notion이 `https://mcp.notion.com`(슬래시 없음)을 돌려주므로 비교 시 정규화합니다.
- **iframe 권한**: 뷰어는 읽기 도구(`search`, `fetch`, `list-*-pages`)만 호출할 수 있습니다. 쓰기 도구는 에이전트(채팅)만 사용합니다.
- **ECS 이미지의 `mcp` SDK**: iframe 프록시가 ECS에서 `mcp` 패키지로 Notion MCP를 호출하므로, 루트 `Dockerfile`의 pip 목록에 `mcp>=2.1.1`이 있어야 합니다. (없으면 뷰어에 `No module named 'mcp'`가 표시됩니다.)
- **배포**: Runtime 이미지(`notion_bearer_auth.py`, `notion_app.py`, `chat.py` 등)와 ECS 웹 이미지를 모두 다시 배포해야 합니다.
