# 가드 인터페이스 (v6, 대회 `POST /guard`)

작성 2026-11-03 · v5 2026-11-09 · v6 2026-11-10 · 담당 danielcho02 · 상태: **초안, 공유 전**(사용자 확인 뒤 리더에게 공유)
근거: [PRD v6 GR-01·GR-09·§9](PRD.md) · [contract/guard.schema.json](contract/guard.schema.json) · [ADR-0009](adr/0009-stateless-http-judge.md) · `competition/SPEC.md` §3·§5

v4의 함수 연결 제안은 커밋 `429e9fb`, v5 판은 커밋 `50d6142`의 이 문서에 있다.

## 1. 채점기 ↔ `/guard`

```
POST /guard
Authorization: Bearer <token>        # submission.json의 token과 같은 값. 서버는 환경변수 TEAM_TOKEN으로 읽는다
Content-Type: application/json

{"spec_version": "1.0", "session_id": "s_…",
 "messages": [{"role": "user", "content": "…"}, …],
 "candidate_answer": "튜터가 내놓으려는 답변"}          # 없을 수 있다
```

```
200 OK
{"input":  {"decision": "allow" | "block", "category": "<범주 10개 중 하나>"},
 "output": {"grounded": true | false, "unsupported": ["답변에서 그대로 옮긴 문자열", …],
            "safe": true | false, "category": "<input.category 복사, 채점 안 함>"}}
```

| 규칙 | 값 |
|---|---|
| 상태 코드 | 인증 실패를 뺀 모든 경우 200. 판정 실패·예산 초과·요청 검증 실패는 기본값 응답(PRD GR-09). FastAPI 기본 422는 입력을 되풀이하므로 쓰지 않는다. 인증 실패는 401(SPEC에 코드 없음, PRD §11-1) |
| 경로 | `/guard` 하나. `/guard/` 리다이렉트 없음(`redirect_slashes=False`) |
| 형식 | JSON 하나, 비스트리밍, 256KB 미만, 소문자 enum 그대로, `null` 없음 |
| `decision` | 범주가 jailbreak_injection·harmful_content·academic_dishonesty·off_topic이면 block(ADR-0012) |
| `output` | `candidate_answer` 키가 있으면 넣는다(빈 문자열이어도) |
| 시간 | 요청 예산 약 20초(규격 30초) |
| 상태 | 없음. 매 요청의 `messages`만 본다. 요청 내용 캐시 없음 |
| 로그 | 시간·길이·상태 코드·오류만(PRD GR-12) |

스키마 검증: 응답은 `contract/guard.schema.json`의 `$defs/response`로 테스트에서 검사한다.

## 2. `/tutor`와 공유할 수 있는 것 (제안, 리더 결정)

`/tutor`는 부적절한 요청에 응하면 그 문항이 0점이다(SPEC §4). 시작용 `competition/server.py`는 `/tutor`와 `/guard`를 한 앱에 둔다(SPEC이 같은 서버를 요구하지는 않음, PRD §9 D4).

| 공유 후보 | 형태 | `/tutor`에서 쓰는 곳 | 결정 전 기본값 |
|---|---|---|---|
| 입력 범주 판정 | `async Guard.classify(messages) -> Category`와 `decision_of(category, block)`(`block` = 설정 `block_categories`, master-plan §4) | action 결정. 튜터 공개 세트의 범주 → action 대응(PRD §9 D3). block 범주이고 확신할 때만 거절 | 가드만 사용 |
| 출력 판정 | `async Guard.judge_output(messages, candidate_answer) -> OutputVerdict` | 생성 직후 자기 답변 거르기: `unsupported` 구간이 든 문장을 지우고 citations 재계산(PRD §9 D1-2) | 가드만 사용 |
| KB 색인 | 기동 때 한 번 만든 BM25 색인 객체 | 튜터 검색 | 가드 자체 색인 |
| 전역 LLM 게이트 | 세마포어 + 지터 재시도, 요청 예산 | 튜터 생성 호출 | 각자 게이트(합계 동시성 주의) |
| kNN 색인 | 공개 400행 TF-IDF | 튜터 범주 판정 | 가드만 사용 |

- 입력 판정을 `/tutor`가 매 요청 부르면 nano 호출이 요청당 1회 늘어난다. 런박스 실측(PRD §11-2) 뒤에 정한다.

## 3. 요청(리더 결정)과 결정 전 기본값

| 요청 | 결정 전 기본값 |
|---|---|
| PRD §9 D1 범주 판정을 `/tutor`가 쓸지 | 가드는 import 가능한 함수로 만든다 |
| PRD §9 D4 전역 게이트 공유 | 가드는 게이트를 주입받을 수 있게 만든다 |
| PRD §9 D5 `TEAM_TOKEN`·제출 체크리스트 | 리더 결정. 개발은 로컬 토큰으로 |

이 문서의 어떤 항목도 팀원에게 일을 배정하지 않는다.
