# Canva MCP — **DISABLED**

> **상태: 비활성.** MCP 목록·스킬 목록·웹 UI에서 제거됨.  
> `application/canva_auth.py`의 `DISABLED = True`.  
> Canva가 배포용 https redirect URI를 허용하면 재활성화할 수 있도록 코드는 유지.

## 비활성 사유 (제약사항)

1. Canva 동적 클라이언트 등록(DCR)은 **루프백(`localhost` / `127.0.0.1`) 콜백만 허용**한다.
2. 공개 도메인(`https://…/api/session/canva/callback`)은 `Invalid redirect URI`로 거부된다.
3. 배포(ECS)에서는 승인 후 `http://localhost:53682/canva/callback?code=…`로 이동하며, 그 주소에는 서버가 없어 연결 거부 페이지가 뜬다.
4. 사용자가 주소창 URL을 복사해 앱에 붙여넣어야 토큰 교환이 끝나는데, **제품 UX로 부적합**하다.
5. Canva 파트너/허용 목록으로 우리 도메인을 등록하면 자동 콜백이 가능할 수 있으나 **미검증**.

Notion·Drive와 달리 배포 환경에서 “한 번 승인으로 끝”이 불가능해 비활성화했다.

## 재활성화 체크리스트

- [ ] Canva가 `sharing_url` 기반 https redirect를 허용하는지 확인(또는 파트너 등록)
- [ ] `canva_auth.DISABLED = False`
- [ ] `application/mcp.list` · `runtime_agent/langgraph/mcp.list`에 `canva` 복구
- [ ] 필요 시 `application/skills.list`에 `canva-*` 스킬 복구
- [ ] Sidebar OAuth footer · `/api/session/canva` 라우트 · config 상태 필드 복구
- [ ] `mcp_config.py`의 canva 분기에서 `return None` 제거

## (참고) 원래 동작 요약

Canva 호스팅 MCP(`https://mcp.canva.com/mcp`) + 사용자별 OAuth(DCR/PKCE).  
배포 시 수동 붙여넣기 콜백: `http://localhost:53682/canva/callback`.  
스킬 원본: [canva-sdks/canva-skills](https://github.com/canva-sdks/canva-skills) → `runtime_agent/langgraph/skills/canva-*`.
