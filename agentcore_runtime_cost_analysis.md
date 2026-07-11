# 💰 AgentCore Runtime Cost 분석

> AWS 공식 문서 기반으로 AgentCore Runtime 과금 기준을 조사한 내용입니다.  
> 참고: https://aws.amazon.com/bedrock/agentcore/pricing/

---

## ✅ 핵심 답변: 15분 비용을 다 내야 하나?

> **NO! 실제 소비한 리소스(CPU·메모리)만큼만 청구됩니다.**  
> 15분(`idleRuntimeSessionTimeout`)은 **과금 단위가 아니라** 세션 종료 기준입니다.

---

## 1. 과금 방식: Active Consumption-Based Pricing

AgentCore Runtime은 전통적인 **pre-allocated 방식(고정 인스턴스 비용)이 아닌**,  
실제 소비한 CPU·메모리만 과금하는 방식입니다.

| 리소스 | 단가 |
|--------|------|
| **CPU** | $0.0895 / vCPU-hour |
| **메모리** | $0.00945 / GB-hour |

**주요 특징**

- 초 단위 계산 (최소 1초)
- **CPU**: I/O wait(LLM 응답 대기, API 대기) 중 CPU를 쓰지 않으면 **CPU 비용 = $0**
- **메모리**: 해당 초까지의 **peak 메모리**로 계산
- 메모리 최소 128MB 적용

> 💡 일반적인 AI 에이전트 워크로드는 30~70%가 I/O wait이므로,  
> pre-allocated 방식 대비 최대 **3.3배 CPU 비용 절감**이 가능합니다.

---

## 2. 15분(idleRuntimeSessionTimeout)의 정확한 의미

15분은 **"세션이 idle 상태로 이 시간이 지나면 microVM을 종료한다"**는 수명 정책이지,  
15분치 비용을 무조건 내라는 의미가 아닙니다.

| 설정값 | 기본값 | 의미 |
|--------|--------|------|
| `idleRuntimeSessionTimeout` | **900초 (15분)** | 세션이 idle 상태로 이 시간이 지나면 microVM 종료 |
| `maxLifetime` | **28800초 (8시간)** | microVM의 최대 수명 |

- idle 중 CPU를 사용하지 않으면 **CPU 비용 없음**
- idle 중에도 메모리를 점유하고 있으면 **메모리 비용 발생**

---

## 3. 과금 범위 (세션 전체 생명주기)

AWS 공식 문서 원문:

> *"You only pay for actual resource consumption during your session, which spans from **microVM boot, initialization, active processing, idle periods, until session termination (microVM shutdown)**"*

즉, idle 시간도 과금 범위에는 포함되지만 **실제 소비량 기반**입니다:

| 상태 | CPU 비용 | 메모리 비용 |
|------|----------|-------------|
| Active (처리 중) | 발생 | 발생 |
| Idle (대기 중) | **없음** (CPU 미사용 시) | 발생 (메모리 유지 시) |
| Terminated (종료) | 없음 | 없음 |

---

## 4. 실제 비용 계산 예시

**조건**: 요청 1건, 세션 60초, I/O wait 70%(42초), 실제 CPU active 18초, 1vCPU, peak 메모리 2.5GB

```
CPU 비용:    18초 × 1vCPU × ($0.0895 / 3600) = $0.000448
메모리 비용: peak 2.5GB 기준               ≈ $0.000276
총 비용:                                   ≈ $0.000724 (약 0.07원/요청)

10M 요청/월 기준: $7,235/월
```

**pre-allocated 방식과 비교**

| 항목 | AgentCore (Consumption) | Pre-allocated |
|------|------------------------|---------------|
| CPU 비용 기준 | 실제 active 18초 | 전체 60초 |
| CPU 비용 절감 | — | 최대 **3.3배** 절감 |
| 메모리 비용 기준 | peak 메모리 | 고정 할당 |
| 메모리 비용 절감 | — | 최대 **1.4배** 절감 |

---

## 5. 비용 최적화 팁

| 방법 | 효과 |
|------|------|
| `idleRuntimeSessionTimeout` 줄이기 (예: 60~120초) | idle 메모리 비용 절감 |
| `StopRuntimeSession` API 명시 호출 | 세션 즉시 종료로 비용 절감 |
| I/O 대기 중 CPU 미사용 | 자동으로 CPU 비용 절감됨 |
| 환경별 차등 설정 (개발/프로덕션) | 불필요한 과금 방지 |

---

## 6. LifecycleConfiguration 설정 가이드

`CreateAgentRuntime` 또는 `UpdateAgentRuntime` API에서 `lifecycleConfiguration` 파라미터로 설정 가능합니다.

```python
import boto3

client = boto3.client('bedrock-agentcore-control', region_name='us-west-2')

# 비용 최적화를 위한 lifecycle 설정 예시
response = client.create_agent_runtime(
    agentRuntimeName='my_agent_runtime',
    agentRuntimeArtifact={
        'containerConfiguration': {
            'containerUri': '123456789012.dkr.ecr.us-west-2.amazonaws.com/my-agent:latest'
        }
    },
    lifecycleConfiguration={
        'idleRuntimeSessionTimeout': 120,   # 2분 (기본 15분 → 비용 절감)
        'maxLifetime': 14400                # 4시간
    },
    networkConfiguration={'networkMode': 'PUBLIC'},
    roleArn='arn:aws:iam::123456789012:role/AgentRuntimeRole'
)
```

**제약 조건**

- `idleRuntimeSessionTimeout` ≤ `maxLifetime` 이어야 함
- 유효 범위: 60 ~ 28800초 (1분 ~ 8시간)

---

## 7. idleRuntimeSessionTimeout을 줄이면 왜 비용이 줄까?

idle 중 CPU는 사용하지 않으므로 CPU 비용은 발생하지 않지만,  
**메모리는 microVM이 살아있는 한 계속 점유**되므로 메모리 비용이 계속 발생합니다.  
`idleRuntimeSessionTimeout`을 줄이면 메모리 점유 시간이 짧아져 비용이 줄어듭니다.

```
요청 처리 완료 (예: 10초)
        │
        ▼
[Idle 상태 진입]
  - CPU: 거의 0     → CPU 비용 없음
  - Memory: 유지 중 → 💸 메모리 비용 계속 발생!
        │
        ▼ idleRuntimeSessionTimeout 경과 후
[microVM 종료] → 메모리 비용 중단
```

**메모리 2GB 세션 기준 idle timeout별 비용 비교**

| idle timeout 설정 | idle 동안 메모리 비용 |
|------------------|----------------------|
| **15분 (기본값)** | 2GB × 900초 × ($0.00945/3600) = **$0.00473** |
| **2분으로 단축** | 2GB × 120초 × ($0.00945/3600) = **$0.00063** |
| **절감액** | **약 87% 감소** |

**상태별 과금 정리**

| 구분 | 과금 여부 | 이유 |
|------|-----------|------|
| CPU (I/O wait 중) | ❌ 없음 | 실제 CPU 연산을 안 함 |
| CPU (idle 중) | ❌ 없음 | 실제 CPU 연산을 안 함 |
| **메모리 (idle 중)** | ✅ **발생** | microVM이 살아있는 한 메모리는 실제로 점유 중 |

> **결론**: "실제 소비한 리소스만 과금" 원칙은 유효하며, idle 중에도 메모리는  
> **실제로 소비되고 있기 때문에** 과금됩니다.  
> `idleRuntimeSessionTimeout`을 줄이면 그 메모리 점유 시간이 짧아져서 비용이 줄어드는 것입니다.

---

## 8. 용도별 권장 설정

| 사용 환경 | Idle Timeout | Max Lifetime | 이유 |
|-----------|-------------|--------------|------|
| **Interactive Chat** | 10~15분 | 2~4시간 | 응답성과 리소스 사용 균형 |
| **Batch Processing** | 30분 | 8시간 | 장시간 작업 허용 |
| **Development** | 5분 | 30분 | 빠른 정리로 비용 절감 |
| **Production API** | 15분 | 4시간 | 표준 프로덕션 워크로드 |
| **Demo/Testing** | 2분 | 15분 | 임시 사용에 공격적인 정리 |

---

## 9. 세션 상태별 동작 정리

```
요청 수신
    │
    ▼
[microVM 부팅 + 초기화] → 비용 발생 시작 (CPU 사용, 메모리 점유)
    │
    ▼
[Active: 요청 처리 중]
    ├─ LLM 응답 대기(I/O wait) → CPU 비용 없음, 메모리 비용만
    └─ 실제 연산 중            → CPU + 메모리 비용
    │
    ▼
[Idle: 처리 완료, 다음 요청 대기]
    ├─ CPU 미사용 → CPU 비용 없음
    └─ 메모리 유지 → 메모리 비용 발생
    │
    ▼ (idleRuntimeSessionTimeout 경과 후)
[microVM 종료] → 비용 발생 중단
```

---

## 10. 결론 요약

1. **요청 1건 처리 후 10초만에 끝났다면 → 10초치 리소스 비용만 납니다**
2. **15분(idle timeout)은 microVM을 얼마나 오래 대기시킬지의 설정값**
3. **idle 동안 CPU를 안 쓰면 CPU 비용은 $0**
4. **idle 동안 메모리를 점유하면 메모리 비용은 발생 → timeout 단축으로 절감 가능**
5. **비용 절감을 위해 `idleRuntimeSessionTimeout`을 짧게 설정 권장**

---

*조사일: 2026-06-29*  
*참고 문서:*  
- *https://aws.amazon.com/bedrock/agentcore/pricing/*  
- *https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html*  
- *https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html*  
- *https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html*
