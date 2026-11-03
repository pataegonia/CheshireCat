# 가드 ↔ 튜터 인터페이스 제안서 (초안)

작성 2026-11-04 · 담당 danielcho02 · 상태: **초안, 공유 전**(사용자 확인 뒤 리더에게 공유)
근거: [PRD GR-11](PRD.md#gr-11-연결-계약과-튜터-쪽-의무-p0) · [master-plan §4.1·§4.2](master-plan.md#4-공통-계약) · [PRD §9 D3·D4](PRD.md#9-튜터팀과-맞닿는-결정-제안-결정은-리더)

이 문서는 가드레일 패키지(`guardrail/`)를 튜터에 붙이는 방법을 **제안**한다. 결정은 리더가 한다. 튜터 쪽이 할 일은 모두 "요청(리더 결정)"이고, 결정이 없어도 가드는 모의 튜터로 모든 수치를 먼저 낸다.

## 1. 연결 방식 두 가지

### (A) 권장: 튜터는 어댑터만 구현하고 가드가 흐름을 쥔다

튜터는 함수 두 개(`retrieve`, `generate`)를 가진 객체 하나만 만든다. 가드가 입력 검사 → 검색 → 생성 → 출력 검사를 모두 조율한다.

```python
from guardrail import Guard, Session

guard = Guard.from_config("configs/default.yaml")
session = Session()                      # 대화 하나에 하나. 턴마다 같은 객체를 넘긴다
verdict = await guard.respond(message, history, tutor_adapter, session=session)
show(verdict.response)                   # 학생 화면에는 이것만 보인다
```

- 튜터 코드 변경이 가장 적다. 병렬 실행(입력 판정과 생성을 동시에), 차단 시 생성 취소, 재생성, 위기 우선 처리를 가드가 맡는다.

### (B) 튜터가 흐름을 쥐고 가드 함수를 부른다

```python
canary = guard.new_canary()              # output.code가 꺼졌거나 obligations.canary가 거짓이면 None
system_extra = guard.system_extra(canary)  # 시스템 프롬프트 끝에 그대로 붙인다(무방비면 "")

iv = await guard.check_input(message, history)      # 검색과 동시에 시작한다
chunks = await tutor.retrieve(message, history)
guarded = guard.guard_retrieval(chunks)             # 생성에는 이 청크를 쓴다

if iv.verdict == "block":                # 최종. 생성하지 않고 iv.response를 그대로 보여 준다
    show(iv.response)                    # 위기 템플릿 치환도 이 안에서 끝난다
else:
    answer = await tutor.generate(message, history, guarded,
                                  system_extra=system_extra, instruction=iv.instruction)
    ov = await guard.check_output(message, history, chunks, answer, iv,   # 원래 청크를 넘긴다
                                  canary=canary, system_prompt=TUTOR_SYSTEM_PROMPT)
    show(ov.response)
```

- `check_input`이 전환·부분 응답이면 `verdict == "modified"`, `response is None`이고 `instruction`에 생성 지시가 있다. 그것을 `generate(instruction=)`에 넘긴다.
- `check_output()`에는 `retrieve()`가 준 **원래 청크**를 넘긴다. 사전 필터의 포함률과 인용 확인이 원문 기준이라서다(가드가 표시를 단 청크가 아님).
- `check_input`의 Verdict는 `check_output`에 그대로 넘긴다. 위기 플래그와 입력 단계 기록이 이것으로 넘어가서, 입력 판정 장애 때 상담 연락처 줄이 붙는다.

## 2. 튜터가 주는 것

```python
class TutorAdapter(Protocol):
    system_prompt: str | None            # 선택
    async def retrieve(self, question: str, history: list[Turn]) -> list[Chunk]: ...
    async def generate(self, question: str, history: list[Turn], chunks: list[Chunk],
                       *, system_extra: str = "", instruction: str | None = None) -> str: ...
```

| 항목 | 뜻 |
|---|---|
| `Chunk(sid, source, text)` | `sid`는 답변의 `[S#]`와 같은 값("S1"). `source`는 출처 표시 |
| `Turn(role, content)` | `role`은 `"student"` 또는 `"tutor"` |
| `system_extra` | 가드가 켠 단계의 문구만 담는다(출력 검사가 켜졌으면 카나리아 지시, 검색 가드가 켜졌으면 datamarking 안내). 시스템 프롬프트 끝에 그대로 붙인다. 무방비 설정이면 빈 문자열 |
| `instruction` | 전환·부분 응답·재생성 지시. GR-11의 `regenerate(feedback)` 대신 `generate(instruction=feedback)`로 받는다 |
| `system_prompt` | 주면 가드가 답변에 시스템 프롬프트 문장이 새는지 이것과도 비교한다(GR-07). 없으면 그 비교만 빠지고 연결은 된다 |

`generate`는 **완성된 답 문자열**을 돌려준다(비스트리밍). 가드 테스트에는 `system_prompt` 속성이 없는 10줄짜리 어댑터가 그대로 연결되는 테스트가 있다(`tests/test_pipeline.py`의 `EchoTutor`).

## 3. 가드가 주는 것: `Verdict`

| 필드 | 뜻 |
|---|---|
| `verdict` | `pass` · `block`(최종, 생성하지 않음) · `modified`(전환·부분·수리) |
| `action` | `allow` · `redirect` · `partial` · `refuse` · `crisis` |
| `category` | S0–S14 (PRD §3) |
| `confidence`, `rule_id`, `reason_code`, `reason` | 판정 근거. **학생에게 보이지 않는다** |
| `latency_ms`, `policy_version` | 지연, 정책 버전 |
| `response` | **학생 화면 문구.** `respond()`·`check_output()`은 통과여도 표시할 답을 넣는다. `check_input()`은 허용이면 `None` |
| `request_id` | 호출마다 새 uuid4. 서비스 모드 로그와 이의 제기가 쓴다 |
| `crisis_tier`, `crisis_flag` | 위기 등급(T1–T3), 위기 어휘 플래그 |
| `instruction` | (방식 B) `check_input`이 전환·부분일 때 생성 지시 |
| `fallback_infra` | 판정 API 장애로 안전 응답을 냈는지 |
| `stages` | 단계별 기록(13개 단계 이름, 지연·토큰·사유 코드). 원문은 담지 않는다 |

위 10개(`verdict`–`response`)가 GR-11 공통 필드다.

## 4. 튜터 쪽 의무 5개와 없을 때의 저하 모드 (GR-11)

| 의무 | 없으면 |
|---|---|
| 답변에 `[S#]` 인용 | 인용 검사·출처 정확도 생략, 문장–청크 근거 판정만 |
| 시스템 프롬프트에 가드가 준 카나리아 삽입(`system_extra`) | 유출 검사는 시스템 프롬프트 문장 매칭만 |
| 청크를 `guard_retrieval`로 감싸 생성에 전달 | 검색 가드 꺼짐(ablation 표에 표시) |
| 비스트리밍, 검사 후 표시 | 가드는 차단 대신 사후 경고만(데모에 쓰지 않음) |
| `regenerate` 콜백(= `generate(instruction=)`) | R2 재생성 생략, R1·R3만 |

설정 `tutor.obligations`에 다섯 값을 적으면 가드가 그에 맞는 저하 모드로 돈다. 방식 (A)에서는 어댑터가 이 의무를 대부분 자동으로 지킨다.

## 5. 켜기·끄기와 버전

- `configs/unguarded.yaml`: 가드 단계를 모두 끈 무방비 모드(전후 비교의 "적용 전"). 튜터 답이 그대로 나온다.
- `configs/default.yaml`: 단계 묶음 4개(입력 · 세션 · 검색 · 출력)와 위기를 토글로 켜고 끈다. 켰지만 아직 구현되지 않은 단계는 시작할 때 오류로 멈춘다(조용히 건너뛰지 않음).
- 버전 확인: `Verdict.policy_version`(정책), trace의 `config_hash`(설정 전체).

## 6. 비용·지연 기대치 (PRD §6)

- 요청당 판정 호출 2회(입력·출력), 동시 호출 2개. 입력 쪽 지연은 생성 뒤에 숨는다.
- 첫 문장 p95 ≤5s는 공동 목표다.
- 튜터가 자기 클라이언트로 생성하면 `async with guard.llm.slot("generator"):`로 감싸 가드와 동시성 한도·호출 예산을 함께 쓴다(엘리스 RPM을 둘이 나눠 쓰기 위해서다. 연결은 GP3 S3.1).

## 7. 오류

- 가드는 단계 오류로 멈추지 않는다. 판정 API가 실패하면 GR-14 등급대로 안전 응답(위기는 템플릿·연락처 우선)을 낸다.
- 방식 (A)에서 튜터 검색·생성 예외는 GP2 S2.5부터 "생성 실패" 안전 응답이 된다(그 전에는 예외가 그대로 올라온다).
- 방식 (B)에서는 튜터가 `retrieve`·`generate`를 직접 부르므로 그 예외는 튜터 쪽에서 처리한다.

### 7-1. 이의 제기(서비스 모드)

학생이 "잘못 막혔어요"를 누르면 그 응답 Verdict의 `request_id`로 `guard.record_appeal(request_id, message)`를 부른다. 그 요청이 30일 보관 대상(`appeal`)이 된다(GR-13). 서비스 모드 로그는 GP2 S2.3에서 만든다.

## 8. 요청(리더 결정)과 결정 전 기본값

| 요청 | 결정 전 기본값 |
|---|---|
| D3 비스트리밍(검사 후 표시)으로 답을 보여 줄지 | 가드 수치는 비스트리밍 기준으로 낸다. 스트리밍이면 사후 경고만 가능하다고 보고서에 적는다 |
| D4 튜터 의무 5개(§4) 중 어디까지 지킬지 | 없는 의무는 저하 모드로 돈다. 모든 수치는 모의 튜터로 먼저 낸다 |
| 연결 방식 (A)·(B) 중 무엇으로 붙일지 | (A)를 권장한다. 11/18 12:00까지 연결이 안 되면 모의 튜터 기준으로 확정하고 보고서에 적는다(master-plan §9) |
| D2 생성 모델 계열 | 엘리스 모델 목록을 보고 정한다. 판정 모델은 생성과 다른 계열(ADR-0004) |
| D5 시험 시간 신호를 줄지 | 신호가 없으면 시험 부정(S2) 탐지는 막지 않고 학습 전환만 한다(REQ-34) |

이 문서의 어떤 항목도 팀원에게 일을 배정하지 않는다. 위 요청은 리더가 정하고, 정해지지 않으면 오른쪽 기본값으로 가드가 계속 진행한다.
