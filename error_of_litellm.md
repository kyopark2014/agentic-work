# LiteLLM Failed Requests 분석

- **대상**: [LiteLLM Usage Dashboard](https://gateway.my-agentic-ai.click/ui/usage/)
- **인프라**: `litellm-guide`로 배포한 Gateway (`https://gateway.my-agentic-ai.click`)
- **분석일**: 2026-07-22
- **데이터 기간**: 대략 최근 1개월 Spend Logs

## Dashboard 수치

| 지표 | 값 |
|------|-----|
| Total Spend | $27.6842 |
| Max Budget | No limit |
| Total Requests | 1,868 |
| Successful Requests | 490 |
| Failed Requests | 1,378 (~73.8%) |
| Average Cost per Request | $0.0148 |
| Total Tokens | 13,295,867 |

성공 490건에만 토큰·비용이 쌓이고, Failed 대부분은 `spend=0` / `tokens=0`이다.

---

## LiteLLM이 Failed를 세는 기준

Usage의 Successful / Failed는 Prometheus가 아니라 **Spend Logs → Daily Spend 집계**다.

| 구분 | 기준 |
|------|------|
| **Successful** | 프록시가 추론 요청을 정상 완료(대개 **2xx**)한 건. UI Successful과 [billable metering](https://docs.litellm.ai/docs/proxy/billing_metrics)이 같은 계열 |
| **Failed** | 클라이언트가 성공 응답을 못 받은 건 — **401/400/403/500** 등 예외·에러로 `status=failure` 기록 |
| **Total** | Successful + Failed |
| **미포함** | ALB/ECS health check (`/health/liveliness` 등), 단순 UI GET 등은 보통 이 카운트에 안 들어감 |

즉 “업스트림 Bedrock 장애”만이 아니라, **키 없음·잘못된 키·모델명 오류·요청 본문 오류**도 전부 Failed로 잡힌다.  
참고: [Endpoint Activity](https://docs.litellm.ai/docs/proxy/endpoint_activity)

---

## 실패 HTTP / 예외 분포 (Spend Logs)

| error_code | 건수 |
|------------|------|
| 401 | 778 |
| 500 | 311 |
| (코드 없음) | 269 |
| 400 | 13 |
| 403 | 7 |

주요 exception class: `HTTPException`, `KeyNotFoundError`, `APIConnectionError`, `ProxyException`, `ProxyModelNotFoundError` 등.

성공 `call_type`: `anthropic_messages`, `responses`, `aresponses`, `acompletion`  
실패 `call_type` / `provider`: 대부분 미설정(`?` / `unknown`) — 인증 단계에서 막혀 routing 전에 끝난 경우가 많음.

---

## 원인별 분류 (Failed 1,378건)

| 원인 | 건수 | 비율 |
|------|------|------|
| `sk-`가 아닌 Authorization / Virtual Key 형식 오류 | ~375 | 27.2% |
| 잘못된·가짜 Virtual Key | ~363 | 26.3% |
| Responses `input` 본문 검증 실패 (Bedrock Mantle) | ~262 | 19.0% |
| API 키 자체가 없음 | ~239 | 17.3% |
| 모델 미등록 / 이름 불일치 | ~36 | 2.6% |
| 클라이언트가 body에 `api_base` 전달 (거부) | ~25 | 1.8% |
| Expired Key | ~22 | 1.6% |
| 스캐너 / SQL injection 프로브 | ~18 | 1.3% |
| encrypted reasoning region scope 등 | ~12 | 0.9% |
| 기타 (프롬프트 한도, `max_output_tokens`, 403 등) | 나머지 | ~2% |

**요약**: Failed의 약 **72%는 인증 실패**, 약 **19%는 Codex/Responses 본문 검증 실패**. 인프라(ECS/ALB) 장애가 주원인은 아니다.

---

## 상세 원인

### 1) 인증 실패 (~72%)

공개 HTTPS 엔드포인트에 대한 잘못된 클라이언트 설정·탐색 트래픽이 Failed를 크게 부풀린다.

반복 메시지 예시:
- `No api key passed in.`
- `LiteLLM Virtual Key expected. Received=..., expected to start with 'sk-'.`
- `Authentication Error, Invalid proxy server token passed.`
- `Authentication Error - Expired Key.`
- `' OR ...` 형태의 스캐너/SQL injection 프로브

### 2) Codex / Responses 본문 문제 (~19%)

`litellm-guide`의 Codex 이슈와 같은 계열.

```
invalid request body: Invalid 'input': value did not match any expected variant
```

성공 로그의 `responses` / `aresponses`와 맞물림. 에이전트/`additional_tools`가 붙은 턴에서 Bedrock Mantle이 body를 거절하면 LiteLLM이 **500 + failure**로 기록한다.

### 3) 모델 alias 불일치

| 구분 | 모델명 패턴 예 |
|------|----------------|
| 실패 | `gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-5.5`, `tts-1` 등 짧은 이름 |
| 성공 | `bedrock_mantle/openai.gpt-5.6-sol`, `bedrock/us.anthropic.claude-haiku-4-5-...` 등 등록된 `model_name` |

클라이언트가 `/v1/models`에 없는 이름을 치면 Failed로 센다.

### 4) 기타

- `api_base is not allowed in request body` — clientside passthrough 미허용
- `encrypted reasoning is scoped to the region...`
- `prompt is too long: ... > 200000 maximum`
- Mantle `openai.gpt-5.5` does not exist 등

---

## 일별 Failed 추이

| 날짜 | Failed |
|------|--------|
| 2026-07-16 | 7 |
| 2026-07-17 | 292 |
| 2026-07-18 | 97 |
| 2026-07-19 | 17 |
| 2026-07-20 | 685 |
| 2026-07-21 | 201 |
| 2026-07-22 | 79 |

---

## 확인 방법

1. Admin UI → **Logs** → `status=failure` 필터
2. API (master/virtual key):

```bash
# 기간별 집계 (UI Usage와 동일 metadata)
curl -sS "$LITELLM_URL/user/daily/activity?start_date=2026-06-22&end_date=2026-07-22&page=1&page_size=1000" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# 개별 Spend Logs
curl -sS "$LITELLM_URL/spend/logs?page=1" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

로그 필드: `status`, `metadata.error_information.error_code`, `error_class`, `error_message`, `model`, `call_type`

---

## 결론

| 질문 | 답 |
|------|----|
| Failed가 많은 이유? | 주로 **인증 실패(키 없음/형식 오류/잘못된 키/프로브)** + **Codex Responses `input` 검증 실패** |
| Bedrock/인프라 장애인가? | **아니오.** 비용·토큰은 Successful 490건 기준으로만 정상 집계됨 |
| LiteLLM 집계 기준? | Spend Log의 `status=success|failure`를 Daily Activity로 합산. non-2xx·예외 = Failed |

개선 방향(참고):
- Virtual Key(`sk-...`)만 클라이언트에 배포하고, master key 노출·만료 키 정리
- 공개 ALB에 WAF / 인증 전 차단으로 스캐너·무키 요청 감소
- Codex는 `wire_api=responses` + Mantle이 허용하는 `input` 스키마 준수 (`litellm-guide` Codex 섹션)
- 클라이언트 모델명을 Gateway에 등록된 `model_name`과 일치
