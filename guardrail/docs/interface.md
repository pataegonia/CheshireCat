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

`/tutor`는 부적절한 요청에 응하면 그 문항이 0점이다(SPEC §4). 시작용 `competition/server.py`는 `/tutor`와 `/guard`를 한 앱에 둔다(SPEC이 같은 서버를 요구하지는 않음, PRD §9 T4).

| 공유 후보 | 형태 | `/tutor`에서 쓰는 곳 | 결정 전 기본값 |
|---|---|---|---|
| 입력 범주 판정 | `async Guard.classify(messages) -> Category`와 `decision_of(category, block)`(`block` = 설정 `block_categories`, master-plan §4) | action 결정. 튜터 공개 세트의 범주 → action 대응(PRD §9 T3). block 범주이고 확신할 때만 거절 | 가드만 사용 |
| 출력 판정 | `async Guard.judge_output(messages, candidate_answer, category: Category | None = None) -> OutputVerdict` | 생성 직후 자기 답변 거르기(`category`가 None이면 먼저 `classify(messages)`를 부른다(nano 호출 +1). 3단 ①과 safe는 그 범주를 쓴다): `unsupported` 구간이 든 문장을 지우고 citations 재계산(PRD §9 T1-2). 넘겨받은 `category`는 다시 확인하지 않는다. 튜터는 classify가 실패하면 주 호출 범주를 넘긴다(#138) | 가드만 사용 |
| KB 색인 | 기동 때 한 번 만든 BM25 색인 객체 | 튜터 검색(#158) | 튜터가 기동 때 가드 `KbIndex`를 같은 설정 `kb`로 따로 만든다 |
| 전역 LLM 게이트 | 세마포어 + 지터 재시도, 요청 예산 | 튜터 생성 호출 | 각자 게이트(합계 동시성 주의) |
| kNN 색인 | 공개 400행 TF-IDF | 튜터 판단 예시 고르기(#141). 튜터는 색인을 따로 만들고 아래 2.1의 정규화·묶음 함수와 GR-05 고르기 규칙을 쓴다 | 색인은 각자, 규칙은 공유 |

- 입력 판정을 `/tutor`가 매 요청 부르면 nano 호출이 요청당 1회 늘어난다. 런박스 실측(PRD §11-2) 뒤에 정한다.

### 2.1 튜터·통합이 지금 import하는 가드 이름

아래 이름의 시그니처·필드·반환형을 바꾸면 튜터·통합이 깨진다. 바꿀 때는 PR 본문에 적고 튜터·통합 담당을 리뷰어로 넣는다. `guardrail/tests/test_shared_api.py`가 호출하는 쪽이 기대는 인자 종류·필드 순서·반환형만 고정해 두므로, 바꾸면 가드 CI에서 먼저 걸린다. 출력 내용(예: `normalize` 결과)은 고정하지 않는다. 표에 없는 가드 내부 이름은 예고 없이 바뀔 수 있다. 테스트 코드의 import는 세지 않는다.

`Guard.classify`·`Guard.judge_output`(#138)은 아직 main에 없다. S2.8에서 들어오면 S2.11에서 이 표와 테스트에 올린다.

| 모듈 | 이름 | 쓰는 곳 |
|---|---|---|
| `guardrail.server` | `create_app`(인자 없는 `guard_factory` 기본값, `tutor_router`·`defaults`·`startups` 키워드). 기동 때 `app.state.cfg`(설정)·`app.state.guard`(`await guard_factory(cfg)`)를 두고 `startups`를 `await fn(app.state)`로 부른다 | `app.py`, `tutor/startup.py`, `tutor/runtime/budget.py`, `tutor/runtime/llm.py`, `tutor/eval/mock_load.py` |
| `guardrail.guard` | `ConstantGuard` | `tutor/eval/mock_load.py` |
| `guardrail.schema` | `Category`, `OutputVerdict` | `integration/contracts.py`, `integration/actions.py`, `tutor/judge/guard_merge.py`(#138) |
| `guardrail.config` | `load_config`, `resolve_path`, `DEFAULT_CONFIG`, `GuardConfig`(타입), 설정의 `paths.public`·`paths.kb`·`block_categories`·`retrieval.max_query_chars`·`kb.ngram`·`kb.k1`·`kb.b`·`budget_s`·`llm.gate`·`llm.timeout_s.read`·`llm.structured_output`·`llm.reasoning_effort`·`llm.verbosity`·`llm.max_completion_tokens` | `tutor/retrieval/search.py`, `tutor/retrieval/index.py`(#158, `retrieval.max_query_chars`·`kb.*`), `tutor/runtime/llm.py`, `tutor/eval/mock_load.py`, `tutor/judge/examples.py`(#141) |
| `guardrail.kb` | `KbIndex(sections, *, ngram, k1, b)`. `sections`는 섹션 ID → 섹션 원문 Mapping, 나머지는 키워드 인자. `chunks`는 `(섹션 ID, 조각)` 튜플(섹션 순서), `scores(query: str) -> list[float]`는 조각 순서. 점수 값은 고정하지 않는다 | `tutor/retrieval/index.py`(#158) |
| `guardrail.preprocess` | `normalize`, `skeleton` | `tutor/judge/conversation.py`, `tutor/judge/signals.py`, `tutor/answer/postprocess.py`, `tutor/judge/examples.py`(#141) |
| `guardrail.signals` | `is_low_signal(target_pre, has_history, hidden)`. 위치 인자 셋, `hidden`은 str(없으면 `""`), 반환 bool. 1턴이거나 숨긴 텍스트가 있으면 거짓 | `tutor/judge/signals.py`(#156) |
| `guardrail.llm` | `ErrorCode`, `LLM`의 `slot()`·`params`·`structured`(`app.state.guard.llm`으로 접근) | `tutor/runtime/llm.py` |
| `guardrail.judges.schemas` | `strict_schema` | `tutor/runtime/llm.py` |
| `guardrail.data` | `PublicRow`, `LabeledRow`, `load_public` | `tutor/judge/examples.py`(#141) |
| `guardrail.clusters` | `build_clusters` | `tutor/judge/examples.py`(#141) |
| `guardrail.knn` | `select_fewshot(ranked, cluster, block, max_examples, per_category)`. 위치 인자 다섯 개, 행은 `id`·`category`만 읽는다(튜터는 자기 `Example`을 넘긴다) | `tutor/judge/examples.py`(#141) |
| `guardrail.eval.cv` | `load_splits` | `tutor/eval/recall.py`, `tutor/eval/run_local.py`(#141). 평가 도구 전용 |

- 공격 범주 목록은 따로 상수로 내보내지 않는다. 설정 `block_categories`를 `load_config()`로 읽는다(#141과 같은 방식).
- `normalize`의 출력이 바뀌면(예: #140 자모 조합) 튜터 판단 예시의 발화와 kNN 순위도 바뀐다. 그런 PR은 본문에 적는다.
- `is_low_signal`의 문자 기준이 바뀌면 튜터의 저신호 직접 clarify(TU-05)도 함께 바뀐다(#156). 그런 PR은 본문에 적는다. 튜터 쪽 구체 사례는 `integration / gate`에서 걸린다.

## 3. 요청(리더 결정)과 결정 전 기본값

| 요청 | 결정 전 기본값 |
|---|---|
| PRD §9 T1 범주 판정을 `/tutor`가 쓸지 | 가드는 import 가능한 함수로 만든다 |
| PRD §9 T4 전역 게이트 공유 | 가드는 게이트를 주입받을 수 있게 만든다 |
| PRD §9 T5 `TEAM_TOKEN`·제출 체크리스트 | 리더 결정. 개발은 로컬 토큰으로 |

이 문서의 어떤 항목도 팀원에게 일을 배정하지 않는다.
