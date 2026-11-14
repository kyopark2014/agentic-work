# Canva MCP (채팅 내 Canva UI)

agentic-work는 Canva의 호스팅 MCP(`https://mcp.canva.com/mcp`)를 사용자별 OAuth로 연결하고, Canva가 제공하는 MCP Apps UI(이미지 생성, 배경 제거, 디자인 생성, 이미지→디자인)를 채팅 안에 그대로 렌더합니다. Notion과 달리 Canva는 표준 `_meta.ui.resourceUri`로 도구와 UI를 연결해 두어서, 별도 뷰어 없이 서버의 UI를 그대로 씁니다.

저장소 [canva-sdks/canva-skills](https://github.com/canva-sdks/canva-skills)는 MCP 서버가 아니라 이 MCP를 쓰는 **스킬 모음**입니다. 그중 6개를 `runtime_agent/langgraph/skills/canva-*`로 가져왔습니다(라이선스: `third_party/canva-skills/`).

## 사용 방법

1. Sidebar → **MCP** → `canva` 체크 → Canva 승인 팝업 (기본은 **읽기 전용**)
   - **배포(ECS) 환경**에서는 승인 후 "사이트에 연결할 수 없음" 같은 페이지가 뜹니다. **정상**입니다. 그 창의 주소창 URL 전체를 복사해 MCP 메뉴의 입력창에 붙여넣고 **연결 완료**를 누르세요. ([아래 "왜 붙여넣기가 필요한가"](#왜-붙여넣기가-필요한가) 참고)
   - 로컬(`localhost`) 실행에서는 자동으로 완료됩니다.
2. 디자인 생성/편집이 필요하면 MCP 드로어 하단의 **쓰기 권한 추가**로 다시 승인합니다.
3. 채팅에서 요청합니다. 예) "이 디자인을 인스타그램/링크드인용으로 리사이즈해줘", "브랜드 키트 검사해줘"

토큰 수명은 4시간이고 refresh token으로 자동 갱신됩니다.

### 권한 (두 단계)

| 단계 | scope |
|------|-------|
| 읽기 (기본) | `profile:read design:meta:read design:content:read folder:read brandtemplate:meta:read brandtemplate:content:read comment:read asset:read brandkit:read` |
| 읽기+쓰기 (**쓰기 권한 추가**) | 위 + `design:content:write folder:write brandtemplate:content:write comment:write asset:write` |

쓰기 도구는 모델이 호출하므로 기본은 읽기만 요청합니다. 권한이 없으면 에이전트가 "쓰기 권한 추가"를 안내합니다.

### 왜 붙여넣기가 필요한가

Canva의 승인 단계는 동적 등록 클라이언트에 **루프백(`localhost`, `127.0.0.1`) 콜백만 허용**합니다. 우리 도메인을 포함한 다른 https 주소와 커스텀 스킴은 `Invalid redirect URI.`로 거부됩니다(직접 확인: `example.com`도 거부, Notion은 제한 없음). 그래서 배포 환경에서는 콜백을 `http://localhost:53682/canva/callback`으로 등록하고, 사용자가 승인 후 주소창의 URL을 붙여넣으면 서버가 `state`(1회용, 사용자 일치 확인)로 코드를 교환합니다. 이 주소에서는 아무것도 수신하지 않으므로 코드가 외부로 전달되지 않습니다.

Canva가 우리 도메인을 허용하도록 하려면 Canva 측 등록(파트너/허용 목록)이 필요합니다. 메타데이터는 `client_id_metadata_document_supported`를 광고하지만, 이 방식으로 비루프백 콜백이 허용되는지는 **미검증**입니다(우리 도메인에 클라이언트 메타데이터 문서를 호스팅해야 시험할 수 있음).

> 호스트 구조(리소스를 호스트가 읽음, 리소스별 CSP, 샌드박스)는 [mcp-apps-host.md](mcp-apps-host.md)를 참고하세요.

## 동작 흐름

```
Web UI → GET /api/session/canva/authorize[?write=true]  (DCR + PKCE, 서버가 URL 생성)
       → Canva 승인 팝업
         · 로컬: GET /api/session/canva/callback (state로 사용자 식별, 토큰 저장)
         · 배포: http://localhost:53682/canva/callback 로 이동(연결 불가 페이지)
                 → 사용자가 주소창 URL 붙여넣기 → POST /api/session/canva/exchange
chat invoke → runtime_mode: canva_auth.get_access_token (만료 임박 시 refresh)
            → AgentCore payload.mcp_oauth_tokens = {"canva": "<token>"}
            → Runtime chat.create_agent → MCPAdapter → https://mcp.canva.com/mcp
              Authorization: Bearer <token>   (mcp_bearer_auth.McpBearerAuth)
            → _meta.ui.resourceUri 가 있는 도구의 ui:// HTML을 자동 수집 (mcp_apps_host)
도구 결과 → agent.py가 mcpApp(HTML + structuredContent) 첨부 → McpAppFrame(iframe)
iframe → POST /api/mcp-apps/call-tool → ECS가 사용자 토큰으로 Canva MCP 호출 (허용 목록)
```

## UI가 붙는 도구

| 도구 | UI 리소스 |
|------|-----------|
| `generate-image` | `ui://canva/generate-image` |
| `remove-background` | `ui://canva/remove-background` |
| `create-design` | `ui://canva/create-design` |
| `separate-image-layers` | `ui://canva/image-to-design` |

`get-design-candidates`, `get-design-thumbnails`, `track-event-lifecycle`은 `ui.visibility: ["app"]`(UI 전용)이라 **모델에서는 숨기고**, iframe이 호스트 프록시로만 호출합니다.

## 주요 파일

| 파일 | 역할 |
|------|------|
| `application/mcp_oauth.py` | 공용 OAuth(DCR/PKCE/토큰 저장·refresh). Notion과 Canva가 공유 |
| `application/canva_auth.py` | Canva 공급자 설정, scope 단계 |
| `application/api/routes_auth.py` | `/api/session/{notion,canva}` 라우트(공용) |
| `application/mcp_apps_host.py` | iframe `tools/call` 프록시 — `CANVA_IFRAME_TOOLS` 허용 목록 |
| `application/security_headers.py` | CSP에 `static.canva.com` 등 허용 |
| `runtime_agent/langgraph/mcp_bearer_auth.py` | 요청마다 Bearer 주입(공용) |
| `runtime_agent/langgraph/mcp_config.py` | `canva` MCP 정의 |
| `runtime_agent/langgraph/chat.py` | 토큰 가드, UI 전용 도구 숨김, 시스템 프롬프트 |
| `application/web/src/mcpOAuth.ts`, `components/OAuthMcpFooter.tsx` | 연결 팝업 / 하단 버튼(공용) |

## 스킬

`canva-brand-check`, `canva-bulk-create`, `canva-design-feedback`, `canva-edit-design`, `canva-implement-feedback`, `canva-resize-for-social-media`. 원본은 도구를 `Canva:start-editing-transaction`처럼 접두사로 부르지만 agentic-work는 접두사 없이 등록하므로 접두사를 제거했습니다. `canva-cli`는 Canva CLI용이라 가져오지 않았습니다. `bulk-create`의 autofill은 Canva Enterprise가 필요합니다.

## 주의

- **CSP**: MCP App iframe(srcdoc)은 ECS 문서의 CSP를 상속합니다. Canva UI는 `static.canva.com`에서 번들/CSS/폰트를 받으므로 `security_headers.py`에 허용되어 있어야 합니다. 없으면 UI가 시작하지 못합니다.
- **iframe 허용 목록**: iframe은 신뢰할 수 없는 페이지 콘텐츠로 취급해 `CANVA_IFRAME_TOOLS`(UI 도우미, 작업 상태 조회, 썸네일, `create-design-from-candidate`)만 호출할 수 있습니다. 편집/삭제 같은 쓰기 도구는 에이전트(채팅)만 사용합니다.
- **콜백 URL**: 로컬은 요청 Host(자동 콜백), 배포 환경은 위의 루프백 주소(붙여넣기)입니다. DCR로 자동 등록됩니다.
- **배포**: Runtime 이미지와 ECS 웹 이미지를 모두 다시 배포해야 합니다.

## 검증 상태

검증됨: OAuth(DCR/PKCE/refresh, 읽기·쓰기 단계 URL), 붙여넣기 흐름(실서버에서 승인 → `exchange` → 토큰으로 프록시 호출 성공, 잘못된 주소/다른 사용자/재사용 거부), 서버가 선언한 UI 리소스 4개와 도구 연결, iframe 샌드박스에서 UI 부팅과 `ui/initialize`·`tools/call` 대화, ECS와 같은 CSP 아래 동작(수정 전에는 스크립트 13건 차단), 프록시 허용 목록.

**미검증**: 실제 디자인 생성/이미지 생성 결과가 UI에서 완성되는 흐름(쓰기 권한과 Canva AI 크레딧이 필요), 허용 목록이 UI가 쓰는 도구를 빠짐없이 포함하는지(번들 분석 기준으로 작성했고 일부 청크는 지연 로드), `McpAppFrame` 안에서의 실제 렌더링.
