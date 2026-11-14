# Google Drive MCP

agentic-work는 Google의 원격 Drive MCP(`https://drivemcp.googleapis.com/mcp/v1`)를 Streamable HTTP로 연결합니다. 사용자별 Google OAuth access token을 웹 UI에서 받은 뒤 AgentCore Runtime invoke payload로 전달합니다.

README 요약: [README.md — Google Drive 연결](./README.md#google-drive-연결)

## 사전 준비 (Google Cloud)

1. API 활성화 (둘 다 필요)
   - Google Drive API (`drive.googleapis.com`)
   - Google Drive MCP API (`drivemcp.googleapis.com`)
2. OAuth consent screen에 스코프 추가
   - `https://www.googleapis.com/auth/drive.readonly`
   - `https://www.googleapis.com/auth/drive.file`
3. Publishing status가 **Testing**이면 **Test users**에 사용할 Google 계정을 등록  
   (미등록 시 `403 access_denied`)
4. 기존 웹 앱 OAuth 클라이언트(`google_client_id`)에서
   - **Use this client for an AI-powered agent** 체크
   - Authorized JavaScript origins에 앱 origin 등록
5. Developer Preview: [Configure the Drive MCP server](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server)

## 사용 방법

1. 웹 UI 로그인 후 Sidebar → **MCP** → `drive` 체크
2. 처음이면 Google Drive 권한 동의 팝업이 뜹니다. MCP 드로어의 **Drive 연결**으로도 가능합니다.
3. MCP 패널에 **「Google Drive 연결됨」** 확인
4. 채팅에서 Drive 파일 검색·읽기 등을 요청합니다. 설정 변경 후에는 **새 task**를 권장합니다.

토큰은 약 1시간 후 만료됩니다. 만료 시 MCP 드로어에서 **다시 연결**하거나, `drive`가 선택된 채팅 전송 시 브라우저가 silent refresh를 시도합니다.

## 동작 흐름

```
Web UI (GSI token client + Drive scopes)
  → POST /api/session/google-drive  (앱에 토큰 저장)
  → chat invoke
  → runtime_mode: 사용자 토큰 조회
  → AgentCore payload.google_drive_access_token
  → Runtime chat.create_agent
  → MCPAdapter → https://drivemcp.googleapis.com/mcp/v1
     Authorization: Bearer <access_token>
```

로그인만으로는 Drive가 붙지 않습니다. **MCP에서 `drive`를 켜고** OAuth 연결이 있어야 도구가 등록됩니다.

## 주요 파일

| 파일 | 역할 |
|------|------|
| `application/google_drive_auth.py` | 사용자별 토큰 저장/조회 |
| `application/api/routes_auth.py` | `/api/session/google-drive` API |
| `application/runtime_mode.py` | invoke 시 토큰 주입 |
| `runtime_agent/langgraph/mcp_config.py` | `drive` MCP 정의 |
| `runtime_agent/langgraph/google_drive_bearer_auth.py` | 요청마다 Bearer 주입 |
| `application/web/src/googleDriveAuth.ts` | GSI Drive 권한 요청 |

## 도구

Google Drive MCP가 제공하는 도구: `search_files`, `list_recent_files`, `get_file_metadata`, `read_file_content`, `download_file_content`, `create_file`, `copy_file`, `get_file_permissions`.

## 문제 해결

| 증상 | 원인 | 조치 |
|------|------|------|
| `403 access_denied` (테스터만 가능) | OAuth Testing + 테스터 미등록 | GCP Test users에 계정 추가 |
| UI는 “연결됨”인데 Drive 도구 없음 | Runtime MCP 로드 실패 또는 session 캐시 | CloudWatch `Failed to load MCP server 'drive'` 확인 후 **새 task** |
| `drivemcp.googleapis.com` **403** | Drive MCP API / AI agent 클라이언트 / 스코프 문제 | API·OAuth 설정 후 **다시 연결** |
| payload에 `drive` 없음 / 토큰 빈 값 | MCP 미선택 또는 미연결 | `drive` 체크 + Drive 연결 |
| `Popup window closed` | OAuth 팝업 중단 | 다시 연결 후 동의 완료 |

로그 예:

```
WARNING drive MCP skipped: google_drive_access_token missing
HTTP Request: POST https://drivemcp.googleapis.com/mcp/v1 "HTTP/1.1 403 Forbidden"
ERROR Failed to load MCP server 'drive': Server returned an error response
```

## 제한

- refresh token / client secret 기반 서버 갱신은 아직 없습니다. GSI access token + UI 재연결에 의존합니다.
- AgentCore Runtime은 앱 디스크의 토큰 파일을 읽지 않으므로, 매 invoke payload로 토큰을 전달합니다.
- OAuth 클라이언트에 AI agent 설정이 없으면 Google이 MCP 요청을 거절할 수 있습니다.
- Drive MCP 로드 실패 후 동일 `runtimeSessionId`는 캐시된 도구 목록을 재사용할 수 있으므로, 설정 수정 후 **새 task**에서 확인하세요.
