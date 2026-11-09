# 가드 인터페이스 (v5, 대회 `POST /guard`)

작성 2026-11-03 · v5 개편 2026-11-09 · 담당 danielcho02 · 상태: **초안, 공유 전**(사용자 확인 뒤 리더에게 공유)
근거: [PRD v5 GR-01](PRD.md#gr-01-http-규격-p0) · [contract/guard.schema.json](contract/guard.schema.json) · [ADR-0009](adr/0009-stateless-http-judge.md) · [OT 사실 §3·§5](competition/ot-facts.md)

v4의 함수 연결 제안(`Guard.respond()`, `TutorAdapter`, 튜터 의무 5개)은 폐기했다. 대회 채점기는 `/guard`를 `/tutor`와 따로 부르므로 가드가 튜터를 감쌀 일이 없다. v4 원문은 커밋 `429e9fb`의 이 문서에 있다.

## 1. 채점기 ↔ `/guard`

```
POST /guard
Authorization: Bearer <token>        # submission.json의 token과 같은 값. 환경변수로 읽는다
Content-Type: application/json

{"spec_version": "1.0", "session_id": "s_…",
 "messages": [{"role": "user", "content": "…"}, …],
 "candidate_answer": "튜터가 내놓으려는 답변"}          # 없을 수 있다
```

```
200 OK
{"input":  {"decision": "allow" | "block", "category": "<범주 10개 중 하나>"},
 "output": {"grounded": true | false, "unsupported": ["답변에서 그대로 옮긴 문자열", …],
            "safe": true | false, "category": "<input.category 복사>"}}
```

| 규칙 | 값 |
|---|---|
| 상태 코드 | 인증 실패를 뺀 모든 경우 200. 판정 실패·검증 오류·마감 초과도 기본값 응답(ADR-0009). 인증 실패 응답은 `SPEC.md` 대조 |
| 경로 | `/guard` 하나. `/guard/` 리다이렉트 없음(FastAPI `redirect_slashes=False`) |
| 형식 | JSON 하나, 비스트리밍, 256KB 미만, 소문자 enum 그대로, `null` 없음 |
| `output` | `candidate_answer`가 있을 때만 넣는다(OT). 빈 문자열일 때만 `SPEC.md` 대조 |
| 시간 | 요청 마감 25초(규격 30초) |
| 상태 | 없음. 매 요청의 `messages`만 본다 |
| 로그 | 시간·길이·상태 코드·오류만(PRD GR-11) |

스키마 검증: 응답은 `contract/guard.schema.json`의 `$defs/response`로 테스트에서 검사한다.

## 2. `/tutor`와 공유할 수 있는 것 (제안, 리더 결정)

`/tutor`도 부적절한 요청에 응하면 그 문항이 0점이다(OT §7). 가드 판정 로직을 `/tutor`가 쓰면 두 엔드포인트가 같은 기준으로 움직인다.

| 공유 후보 | 형태 | `/tutor`에서 쓰는 곳 | 결정 전 기본값 |
|---|---|---|---|
| 입력 판정 | `judge_input(messages) -> (decision, category)` 비동기 함수 | action 결정(예: `answer_seeking` → `scaffold`, `emotional_distress` → `escalate`, 막을 입력 → `refuse`). 민감해 보여도 정상인 질문을 `refuse`·`redirect`로 처리하면 action 점수를 잃는다(OT p.10). 대응은 공개 세트로 정함 | 가드만 사용 |
| KB 색인 | 기동 때 한 번 만든 BM25 색인 객체 | 튜터 검색 | 가드 자체 색인 |
| 정규화 | `normalize()`, `skeleton()` | 튜터 입력 전처리 | 가드만 사용 |
| 동시성 게이트 | `llm.py`의 게이트 하나 | 튜터 생성 호출 | 프로세스가 나뉘면 각자 |

- 한 서버(FastAPI, uvicorn 프로세스 1개)에 `/tutor`와 `/guard`를 두면 게이트·색인을 공유한다(PRD §9 D2·D4). 가드는 `APIRouter`로 만들어 어느 서버에도 붙게 한다.
- 입력 판정을 `/tutor`가 매 요청 부르면 nano 호출이 늘어난다. 프록시 처리량 측정(PRD §11-1) 뒤에 정한다.

## 3. 요청(리더 결정)과 결정 전 기본값

| 요청 | 결정 전 기본값 |
|---|---|
| D1 가드 판정 로직을 `/tutor`가 재사용할지 | 가드는 import 가능한 함수로 만든다. 쓰지 않아도 가드는 그대로 돈다 |
| D2·D4 한 서버·한 프로세스로 둘지 | 가드는 라우터로 만들어 어느 쪽이든 붙는다 |
| 팀 토큰·키 이름(`kosa_{팀 이름}`)·git 방식 | 리더 결정. 가드 개발은 로컬 토큰으로 |

이 문서의 어떤 항목도 팀원에게 일을 배정하지 않는다.
