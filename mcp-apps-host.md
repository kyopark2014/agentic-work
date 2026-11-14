# MCP Apps 호스트 (채팅 안 인터랙티브 UI)

agentic-work는 MCP 서버가 제공하는 UI(`ui://` 리소스)를 채팅에 렌더합니다. 호스트 계약과 디자인은 Claude의 MCP Apps 문서를 따릅니다.

- [Design guidelines](https://claude.com/docs/connectors/building/mcp-apps/design-guidelines) · [Quickstart](https://claude.com/docs/connectors/building/mcp-apps/quickstart) · [Display modes](https://claude.com/docs/connectors/building/mcp-apps/display-modes) · [Theming](https://claude.com/docs/connectors/building/mcp-apps/transparent-theming)

## 구조

```
서버 도구 결과 ── _meta.ui.resourceUri ──▶ Runtime(agent.py)이 mcpApp 페이로드 첨부
                                           {resourceUri, mcpServer, tool, toolInput, toolResult}   ← HTML 없음
Web(McpAppFrame) ── GET /api/mcp-apps/resource ──▶ ECS가 ui:// 리소스를 한 번 읽어 캐시
                                           {html, meta:{csp, prefersBorder}}
        iframe(srcdoc, sandbox, 리소스별 CSP)  ◀── ui/initialize · tool-input · tool-result
        iframe → tools/call ──▶ POST /api/mcp-apps/call-tool ──▶ 허용 목록 검사 후 서버 호출
```

| 서버 | UI 출처 | 인증 | iframe이 부를 수 있는 도구 |
|---|---|---|---|
| `weather` | 내장 (`application/mcp_server_weather.py`) | 없음 | 날씨 조회 |
| `notion-remote` | `application/mcp_app_views/` (자체 뷰어) | 사용자 OAuth | 읽기 6개 |
| `canva` | mcp.canva.com의 `ui://canva/*` | 사용자 OAuth | UI 헬퍼 9개 |
| `use-vault` | use-vault MCP의 `ui://vault/viewer.html` | ECS 태스크 역할(SigV4) | 읽기 6개, `actor_id`는 세션 사용자로 **강제** |

`runtime_agent/langgraph/mcp_apps_host.py`의 `HOST_FETCHED_SERVERS`에 있는 서버는 페이로드에 HTML을 싣지 않습니다. 예전 메시지에 HTML이 들어 있으면(legacy) 그대로 렌더합니다.

## 호스트가 지키는 계약 (`web/src/components/McpAppFrame.tsx`, `web/src/mcpAppHost.ts`)

| 항목 | 구현 |
|---|---|
| 스타일 | `hostContext.styles.variables` 76개 토큰(이름은 Claude 계약, **값은 이 앱 테마에서**). 테마 전환 시 `host-context-changed` |
| 배경 | iframe 투명, `color-scheme` 일치, `prefersBorder`(기본 없음) |
| 생명주기 | `ui/initialize` → `initialized` → `tool-input` → `tool-result` |
| 표시 모드 | `inline` / `fullscreen`(채팅 영역 오버레이, 컴포저는 계속 보임). `ui/request-display-mode`, `containerDimensions`, `safeAreaInsets`. 전환해도 iframe은 다시 로드되지 않음. 진입 시 호스트 닫기 버튼에 포커스(Esc) |
| 보안 | `sandbox`에서 `allow-same-origin` **제외**(앱 오리진 쿠키/`/api` 접근 불가), 리소스가 선언한 `_meta.ui.csp`만 허용하는 `<meta>` CSP 주입 |
| 크기 | 인라인 높이는 앱이 보고한 값(상한 있음), 로딩 중 스켈레톤 |

## 새 MCP App 추가

1. 서버: 도구에 `_meta.ui.resourceUri`(Python은 `mcp.server.apps.Apps`), 결과는 `CallToolResult(content=[요약], structured_content={...})`. dict를 그냥 반환하면 `structuredContent`가 없고 str은 `{"result": ...}`로 감싸집니다.
2. UI: 공식 `App` 클라이언트 + 호스트 토큰. 이 저장소는 `application/mcp_app_views/`(`python3 build.py`로 단일 HTML 생성, 산출물 커밋)와 공유 키트 `kit.js/css`를 씁니다.
3. 호스트: `mcp_app_resources.py`에 리소스 소스와 `ui://` 접두사, `mcp_apps_host.py`에 iframe 허용 도구 목록, `HOST_FETCHED_SERVERS`에 서버 이름.
4. iframe이 부를 도구는 **명시적 허용 목록**만(쓰기 도구 제외). 사용자 식별 인자는 서버에서 덮어씁니다.

## 디자인 규칙 요약 (이 저장소의 뷰어 기준)
인라인은 카드/캐러셀(3~8개, 높이 자동, 중첩 스크롤 없음, 버튼 2개 이하), 상세·그래프·탐색은 전체 화면. 텍스트 입력과 수정 요청은 채팅, 앱에서는 이미 보이는 데이터의 필터·펼침·선택만. 드롭다운 대신 세그먼트 버튼, 탭 44px, 하드코딩 색 금지.

## 배포 순서
1. **ECS 웹 이미지**(새 호스트와 `/api/mcp-apps/resource`) → 2. **Runtime 이미지**(페이로드에서 HTML 제거, `toolInput`) → 3. **use-vault 런타임**(ob-note 저장소, UI 포함).
새 Runtime + 옛 웹은 UI가 뜨지 않습니다. 옛 Runtime + 새 웹은 정상입니다(legacy 페이로드).

## 검증 상태와 한계
- 검증: 실제 브라우저(헤드리스 Chrome)에서 `McpAppFrame` + 공식 클라이언트로 생명주기·스타일·표시 모드·CSP·샌드박스·테마를 확인했고, Notion/vault 뷰어(+XSS 입력), Canva/weather 실제 UI의 부팅, 실서버 Notion/Canva 호출, 배포된 use-vault 런타임에 SigV4 연결을 확인했습니다.
- **미검증**: 배포 환경의 실제 채팅 화면(ECS+AgentCore) 종단 시험, Canva 생성 결과가 UI에서 완성되는 흐름, 배포된 use-vault 런타임의 UI(재배포 전).
- 사이드 패널은 Claude의 일부 조직 기능이라 구현하지 않았습니다(전체 화면 오버레이만).
