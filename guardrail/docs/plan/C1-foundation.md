# C1 기반·첫 제출 — 세부 플랜

작성 2026-11-10 · 담당 danielcho02 · 상태: v1 (master-plan v23 기준)
상위: [master-plan](../master-plan.md) · 요구사항: [PRD v6](../PRD.md) · 규격: [SPEC](../../../competition/SPEC.md) · 기간: 11/11(수)–11/12(목) 오전

## 1. 목표와 관문

**보이는 변화**: 런박스 터널 주소의 `/guard`가 인증을 확인하고 어떤 입력에도 200 JSON(상수 판정)을 낸다. 그 서버로 첫 제출을 한다. 로컬에서는 채점 코드를 import한 채점기가 `score_local.py`와 같은 값을 낸다. nano의 실제 지연과 파라미터 수용 여부를 표로 안다.

**관문**([master-plan §2](../master-plan.md#2-일정과-phase-관문)): ① 터널 주소로 200 JSON, 3xx 0, 형식 오류 0 ② 채점 재현 차 ≤0.005(같은 저장 응답 기준) ③ PRD §11-2-1 실측 표 ④ CI green.

이 Phase는 판정 품질을 다루지 않는다. 상수 응답(`allow`·`normal`, `true/[]/safe=true`)이 기준선이다.

## 2. 날짜표

| 날짜 | 본인(Opus 세션) | 병렬(Sonnet 서브에이전트, worktree) | 끝나면 |
|---|---|---|---|
| 11/11 수 오전 | S1.1(C1 에픽 #42·이슈 #46–#51은 11/10에 만들었고 v4 에픽 #2–#6·이슈 #15–#21도 11/10에 닫았다, [master-plan §11](../master-plan.md#11-진행-추적-github)) | — | S1.1 PR 머지 |
| 11/11 수 오후 | S1.2(서버 골격) → 보안 리뷰 | S1.3(LLM 게이트) · S1.4(로더·채점·분할) | S1.2 PR |
| 11/11 수 밤 | S1.5 런박스 배포·첫 제출 | — | 첫 제출 기록 |
| 11/12 목 오전 | S1.3·S1.4 리뷰·머지 → S1.6 실측 → Phase 끝 점검 | — | 관문 판정, C2 시작 |

합계 20h(본인 약 12h + 병렬 8h). S1.3·S1.4는 S1.1 머지 뒤에만 시작한다(설정 모델을 같이 쓴다).

## 3. 슬라이스

### S1.1 v4 코드 정리·의존성·설정 v6

| 항목 | 내용 |
|---|---|
| GR / ADR | GR-11, GR-14 |
| 우선 · 예상 · 모델 | P0 · 3h · Sonnet |
| 선행 | — |
| 브랜치 | `gr/s1.1-cleanup-config` |
| 보이는 변화 | `uv run pytest`가 v6 설정 테스트만으로 green. `load_config("configs/default.yaml")`가 §4.4 모델을 돌려준다 |

**배경**: v4 코드는 튜터 래퍼 구조다. v6는 상태 없는 판정기라 대부분이 필요 없다. 처리 목록은 [master-plan §3 기존 코드 처리](../master-plan.md#3-아키텍처--코드).

**만들거나 바꿀 파일**
- 삭제: `src/guardrail/{pipeline,trace,types}.py`, `src/guardrail/eval/{build_grounding,metrics,datasets,run}.py`, 해당 테스트(`test_pipeline`·`test_trace`·`test_build_grounding`·`test_metrics`·`test_datasets`·`test_run`), `configs/unguarded.yaml`, `prompts/attack_judge.md`, `resources/`, `data/seed/`, `data/README.md`, `results/`(v4 파일), `tests/fixtures/mini*`
- 남김: `eval/mock_tutor.py`와 그 테스트(S2.2가 BM25를 옮긴 뒤 지운다), `preprocess.py`(S2.1)
- `eval/stats.py`: `wilson`만 남기고 `test_stats.py`도 줄인다
- `config.py`: 로더·`overrides`·`${ENV}` 치환·`config_hash`·`load_prompt`·`resolve_path` 유지. 모델을 [§4.4](../master-plan.md#44-설정-configsyaml)로 바꾼다. `check_toggles` → 토글 조합 검증(`third_call`은 `knn_vote`·`lr` 둘 다 켰을 때만, `nano_span`은 `literal_rule`을 켰을 때만)
- `configs/default.yaml`(§4.4 그대로), `configs/baseline.yaml`(토글 11개 모두 false + `output.mode: baseline`: 출력은 베이스라인 프롬프트의 nano 값을 grounded·safe에 바로 쓰고 `unsupported=[]`, 입력은 `input_judge.md`를 예시 블록만 비워 쓴다. 정의와 대조 문구는 그대로이고 따로 파일을 두지 않는다. `output.failure_default`는 토글이 아니다), `configs/ablation/`(빈 폴더 + `.gitkeep`)
- `src/guardrail/__init__.py`: 재노출 정리
- `pyproject.toml`·`uv.lock`: 런타임 `fastapi==0.162.2`, `uvicorn==0.54.0`, `scikit-learn==1.9.1`, `rapidfuzz==3.14.6` / dev `jsonschema==4.26.0`. `description`을 "대회 POST /guard 판정기"로
- `.gitignore`: v4 데이터 규칙 삭제, `runs/`·`logs/`·`.env` 유지

**테스트 먼저**: 설정 로드(default·baseline), 모르는 키 거부, `${ENV}` 치환, `config_hash`가 프롬프트 version을 포함, 금지 토글 조합 2개, ablation 깊은 병합(덮어쓴 키만 달라짐).

**수용 기준**
- [ ] 삭제 목록이 모두 반영되고 `rg "(from|import) guardrail\.(pipeline|trace|types)" src tests`에 v4 import가 없다
- [ ] `uv sync --locked`, `ruff`, `pytest --cov --cov-fail-under=80` green
- [ ] `uv lock`이 풀린다(numpy·scikit-learn·Python 3.12). 안 풀리면 버전을 낮추고 PR에 적는다

**스킬·에이전트**: `ecc:tdd-workflow`, `ecc:refactor-clean`. 리뷰 `ecc:python-reviewer`.

### S1.2 서버 골격·스키마 (인증·200 보장·로그 규칙)

| 항목 | 내용 |
|---|---|
| GR / ADR | GR-01, GR-09(파싱 실패 행), GR-12 · [ADR-0009](../adr/0009-stateless-http-judge.md) |
| 우선 · 예상 · 모델 | P0 · 5h · Sonnet + `ecc:security-reviewer` |
| 선행 | S1.1 |
| 브랜치 | `gr/s1.2-server-skeleton` |
| 보이는 변화 | `uvicorn guardrail.server:app --port 8000 --no-access-log`가 뜨고 `/guard`가 상수 판정을 낸다 |

**만들 파일**: `src/guardrail/schema.py`, `src/guardrail/server.py`, `tests/test_schema.py`, `tests/test_server.py`.

**계약**([master-plan §4.1](../master-plan.md#41-타입-schemapy와-모듈별-dataclass))
- `schema.py`: `Category`, `Decision`, `decision_of(category, block)`(표는 설정 `block_categories`, ADR-0012), `Message`, `GuardRequest`, `parse_request(body: bytes) -> GuardRequest`, `OutputVerdict`, `GuardResponse.to_json(block)`(block = 설정 `block_categories`, `frozenset[Category]`).
- `parse_request`는 예외를 내지 않는다. JSON이 아니거나 `messages`가 목록이 아니면 `messages=()`. 메시지 항목 중 `content`가 문자열인 것만 받는다. `candidate_answer` 키가 있으면 `has_candidate=True`. 값이 문자열이면 그대로 두고(빈 문자열 `""`은 출력 `true/[]`·`safe=true`), 문자열이 아니면 `candidate_answer=None`으로 둔다. 이 경우는 출력에 한해 요청 파싱 실패다: `output`은 `output.failure_default`(GR-09), `safe`는 GR-08 그대로.
- `server.py`: `router = APIRouter()`의 `POST /guard`, `create_app(guard_factory) -> FastAPI`(`redirect_slashes=False`, lifespan에서 `Guard` 생성, 같은 런박스 제출을 위해 시작용 `/tutor`도 붙임, S1.5), 모듈 수준 `app = create_app(...)`. `/tutor`는 시작용 라우트를 팀 모듈에 **복사해** 붙인다(`competition/server.py`는 import 때 환경변수를 읽고 자기 앱을 만들므로 import하지 않는다). 아래 인증은 시작용 server.py처럼 **앱 수준 의존성**으로 걸어 `/guard`와 `/tutor`에 똑같이 적용한다.
  - 본문은 `await request.body()`로 직접 읽는다. pydantic 요청 모델을 라우트 인자로 두지 않는다(422 반사 방지).
  - 인증: `Authorization` 헤더를 `f"Bearer {TEAM_TOKEN}"`와 `hmac.compare_digest`로 비교(바이트). 틀리면 401 + `{"error": "unauthorized"}`(SPEC은 코드를 정하지 않는다. 시작용 `competition/server.py`의 `HTTPException(401)`이 근거). `TEAM_TOKEN`이 없으면 기동 실패.
  - 예외 처리기: 라우트 안의 어떤 예외도 GR-09 기본 응답 200으로. 전역 422 처리기도 같은 기본 응답(본문 반사 없음).
  - 이 슬라이스의 판정기는 상수다: `Guard`가 아직 없으므로 `ConstantGuard.judge()`가 `GuardResponse("normal", OutputVerdict(True, (), True) if has_candidate else None)`.
  - 로그 미들웨어: 한 줄 = 시각, 경로, 요청 바이트 수, 응답 바이트 수, 상태 코드, 처리 ms, 오류 클래스 이름. 그 밖은 남기지 않는다(GR-12). `print`·`logger.exception` 금지.
- 응답 크기 256KB 미만 확인(`unsupported`가 길어질 때 대비해 `to_json`에서 상한 검사, 넘으면 `unsupported`를 비운다).

**테스트 먼저**(FastAPI `TestClient`, 요구하면 dev `httpx`)
- 공개 세트 400행 전부 → 200, `contract/guard.schema.json`(jsonschema)로 검증, `candidate_answer` 키 유무에 따라 `output` 유무
- 퍼징(`hypothesis`): 빈 본문, JSON 아님, `messages` 누락·타입 오류, 깨진 유니코드, 매우 긴 이력(1MB) → 모두 200·스키마 통과
- 인증: 헤더 없음·틀림·`Bearer ` 접두 없음 → 401, 본문에 토큰 없음. `create_app()`의 `/tutor`도 같은 경우에 401
- `/guard/`(끝 슬래시) → 3xx가 아님(404 허용)
- 반사: 요청에 넣은 표지 문자열이 오류·기본값 경로의 응답과 `input` 객체에 없음. `unsupported`는 후보 답변의 원문 구간이라 이 검사에서 뺀다
- 로그: `caplog`로 퍼징 전체를 돌린 뒤 로그에 요청 표지 문자열 0건

**수용 기준**
- [ ] 위 테스트 green, 형식 오류 0·422 0·3xx 0·반사 0(GR-01 수용 기준. 반사 검사 범위는 오류·기본값 경로와 `input` 객체, `unsupported`는 예외)
- [ ] 로그 줄의 필드가 위 7개뿐(GR-12)
- [ ] `ecc:security-reviewer` CRITICAL·HIGH 0

**스킬·에이전트**: `ecc:fastapi-patterns`, `ecc:security-review`, 에이전트 `ecc:fastapi-reviewer`·`ecc:security-reviewer`.

### S1.3 LLM 게이트 개편

| 항목 | 내용 |
|---|---|
| GR / ADR | GR-10 · [ADR-0011](../adr/0011-llm-call-structure.md) |
| 우선 · 예상 · 모델 | P0 · 4h · Sonnet |
| 선행 | S1.1 |
| 브랜치 | `gr/s1.3-llm-gate` |
| 보이는 변화 | `LLM.complete()`가 마감 안에서만 1회 재시도하고, 실패는 오류 코드로 돌려준다 |

**배경**: 기존 `llm.py`(663줄)는 v4 역할 5개, AIMD, 시도 3회, `faults`를 가진다. v6 계약은 [master-plan §4.3](../master-plan.md#43-llm-호출-llmpy). 재시도 대기 파싱(`_parse_retry_after`)과 오류 분류(`classify`)는 그대로 쓴다.

**바꿀 파일**: `src/guardrail/llm.py`, `tests/test_llm.py`(다시 씀), `tests/fakes.py`(`FakeLLM`·`FakeClock`만).

**작업**
- `Role = Literal["input_judge", "output_judge"]`. `types.py`가 사라졌으므로 `Role`은 `llm.py`에 둔다.
- `AsyncOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, timeout=Timeout(connect=3, read=12, write=5, pool=5), max_retries=0)`를 `LLM` 생성 때 하나 만든다. 환경변수 `GUARD_LLM_*`는 지운다.
- 요청 인자: `model`, `messages`, `reasoning_effort`, `verbosity`, `max_completion_tokens`, `response_format`. 설정에서 `null`인 인자와 `probe()`가 끈 인자는 보내지 않는다. 보내지 않을 인자(`temperature`·`max_tokens`·`logprobs`)를 테스트로 고정한다.
- `response_format`: json_schema 모드는 `{"type": "json_schema", "json_schema": {"name", "schema", "strict": true}}`, json_object 모드는 `{"type": "json_object"}` + 시스템 끝에 스키마 설명.
- `probe()`: 짧은 고정 메시지로 `reasoning_effort`·`verbosity`·`max_completion_tokens`·`response_format` json_schema를 **하나씩 따로** 시험하고, 거절(400)된 파라미터만 끈다. json_schema가 거절되면 `json_object`로 내린다. 거절이 아닌 오류면 그 파라미터를 유지한다. 결과는 기동 때 파라미터별 오류 코드로만 기록한다.
- 게이트: `asyncio.Semaphore(cfg.gate)`. 대기가 마감을 넘으면 `timeout`. AIMD·`increase_after`·`max_wait_s` 삭제.
- 재시도 1회: 재시도 대상 오류이고 `now + 대기 + read 한도 ≤ deadline`일 때만. 스키마 불일치(`bad_json`)도 1회 다시 묻는다.
- `max_calls`(하네스 예산). `faults`·`FAKE_OK`·`_fake_*` 삭제.
- `slot()`: 같은 세마포어를 쓰는 비동기 컨텍스트(튜터 제안용). 마감 없음.

**테스트 먼저**(`FakeLLM` + 주입 시계, 0ms): 정상, `probe()`가 거절된 파라미터 하나만 끔(나머지는 유지), `null` 파라미터 미전송, 429 → `retry-after` 대기 후 성공, `retry-after`가 마감 넘음 → 바로 `rate_limit`, 타임아웃 → 재시도 → 실패, 400 → 재시도 없음, 잘못된 JSON → 1회 재요청, 게이트 포화 시 마감 초과 → `timeout`, `CancelledError` 전파와 허가 반환, 예산 초과 → `budget`(호출 0), 보내는 인자 목록, 오류 문자열에 예외 메시지가 없음.

**수용 기준**
- [ ] 위 테스트 green, `llm.py` 커버리지 ≥90%
- [ ] `-m live` 스모크 1개(키가 있을 때만): `probe()` + 짧은 `InputOut` 호출

**스킬·에이전트**: `ecc:python-testing`, `ecc:cost-aware-llm-pipeline`. 리뷰 `ecc:silent-failure-hunter`.

### S1.4 공개 세트 로더·채점 재현·짝 묶음 분할

| 항목 | 내용 |
|---|---|
| GR / ADR | GR-13 · [ADR-0006](../adr/0006-evaluation-scope.md), [ADR-0013](../adr/0013-public-set-knn-and-leakage.md) |
| 우선 · 예상 · 모델 | P0 · 4h · Sonnet(분할 규칙은 Opus 검토) |
| 선행 | S1.1 |
| 브랜치 | `gr/s1.4-data-score-cv` |
| 보이는 변화 | `python -m guardrail.eval.cv --write`가 `splits/v1.json`을 만들고, 상수 응답 점수가 공개 세트에서 재현된다 |

**만들 파일**: `src/guardrail/data.py`, `src/guardrail/clusters.py`, `src/guardrail/eval/score.py`, `src/guardrail/eval/cv.py`, `src/guardrail/eval/splits/v1.json`, `tests/test_data.py`, `tests/test_score.py`, `tests/test_cv.py`.

**계약**([master-plan §4.5](../master-plan.md#45-데이터-datapy-evalcvpy))
- `data.py`: `PublicRow`, `LabeledRow`, `load_public(public_dir)`, `labeled_rows(rows)`. `LabeledRow.text`는 마지막 `user` 발화의 `preprocess.normalize()` 결과, `category`는 `labels["category"]`(가드·튜터 둘 다 있음, R13 M3).
- `eval/score.py`: `competition/grader`와 `competition/harness`를 `sys.path`에 넣고 `scoring.score_guard`·`scoring.guard_score`, `score_local.as_scoring_input`을 import한다. `score_responses(rows, responses: dict[id, dict]) -> dict`(가드 점수, block F1, 범주 정확도, 출력 평균과 항목별). `score_guard`·`guard_score`에는 `endpoint=='guard'` 행만 넣고, 가드 점수는 가드 200행으로 보고한다. 튜터 행(decision 라벨 없음)은 범주 정확도와, 범주에서 도출한 decision으로 block F1을 따로 계산한다. 채점 로직을 다시 쓰지 않는다.
- `eval/cv.py`: `build_clusters(rows, jaccard=0.5) -> dict[id, cid]`(서버 쪽 모듈 `src/guardrail/clusters.py`에 두고 `cv.py`가 import한다. S2.5의 `KnnIndex`도 기동 때 같은 함수로 묶음을 계산하고, 서버는 eval 패키지에 의존하지 않는다. **마지막 학습자 발화** 정규화본의 문자 3-gram Jaccard, union-find. 빠른 질문 버튼 첫 발화는 여러 행이 같은 문장을 써 무관한 행을 이으므로 넣지 않는다), `make_splits(rows, folds=5, holdout=0.2, seed)`, `load_splits()`, `train_rows(split, fold=k | holdout)`(평가 묶음과 홀드아웃 묶음을 뺀 `LabeledRow`). CLI `--write`.
- 홀드아웃은 가드 행 기준 20% 묶음. 튜터 행만 있는 묶음은 홀드아웃에 넣지 않는다.

**테스트 먼저**
- 400행 로드, `candidate_answer` 키가 있는 가드 행 수 86
- 상수 응답(`allow`·`normal`·`true/[]/true`)의 가드 점수가 R13 M7 "전부 allow" 값과 같다(소수 둘째 자리)
- 짝 예시(R13-A §1.1의 "최대 ↔ 최소" 쌍)가 같은 묶음
- 빠른 질문 버튼 첫 발화만 같은 행끼리는 그것 때문에 묶이지 않음
- 같은 묶음이 두 폴드에 걸치지 않음, 홀드아웃과 폴드가 겹치지 않음, `seed`가 같으면 같은 파일
- `train_rows(fold=k)`에 폴드 k·홀드아웃 묶음의 id가 없음

**작업 순서**: 테스트 → `data.py` → `score.py` → `cv.py` → `--write`로 파일 생성 → 묶음 수·크기 분포·**가장 큰 묶음 크기**·홀드아웃 행 수를 PR에 적는다 → Opus가 분할 규칙만 검토.

**수용 기준**
- [ ] 위 테스트 green
- [ ] 로컬에서 상수 서버(S1.2의 `ConstantGuard`)를 띄워 같은 저장 응답을 `score_local.py`와 `score.py`에 넣어 가드 점수 차 ≤0.005(GR-13. `guard_score`가 소수 둘째 자리로 반올림하므로 같은 저장 응답이면 실제로는 완전히 같아야 한다), 결과를 이 문서 §4에 적는다
- [ ] `splits/v1.json` 커밋, 이후 바꾸지 않는다

**스킬·에이전트**: `ecc:eval-harness`, 에이전트 `ecc:mle-reviewer`(누수 점검).

### S1.5 런박스 배포·상수 응답 첫 제출

| 항목 | 내용 |
|---|---|
| GR / ADR | GR-01, GR-14 · ADR-0009 |
| 우선 · 예상 · 모델 | P0 · 2h · 본인 |
| 선행 | S1.2 |
| 라벨 | `guardrail`, `guardrail:P0`, `orchestrator` |
| 보이는 변화 | `https://<런박스>.tunnel.elice.io/proxy/8000/guard`가 200 JSON을 낸다. 첫 채점 결과 메시지를 받는다 |

**작업**
- [ ] 런박스 확인: Python 버전(≥3.12인가), PyPI 설치 가능 여부(`uv sync`), 저장소 clone 방법. 비공개 저장소 접근 토큰은 런박스에 남기지 않는다(clone 뒤 지운다). 결과를 §4에 적는다
- [ ] `/home/elicer/competition/.env`에 `OPENAI_API_KEY`, `TEAM_TOKEN`(영문·숫자·기호, 키와 다른 값). 커밋하지 않는다
- [ ] 런박스의 KB·공개 세트·프롬프트 경로가 저장소 상대 경로와 다르면 `GUARD_KB_DIR`·`GUARD_PUBLIC_DIR`·`GUARD_PROMPTS_DIR`로 `paths.*`를 덮어쓴다([master-plan §4.4](../master-plan.md#44-설정-configsyaml)). 설정 파일은 고치지 않는다
- [ ] 실행: `cd guardrail && set -a && . /home/elicer/competition/.env && set +a && uv run uvicorn guardrail.server:app --host 0.0.0.0 --port 8000 --no-access-log`
- [ ] `/tutor`: 각 계정의 제출은 **그 계정 런박스의** `/tutor`·`/guard` 주소여야 한다(SPEC §7). 그래서 상수 응답 제출에도 이 런박스의 `/tutor`가 있어야 한다. `create_app()`은 `/guard` 옆에 시작용 `/tutor`(`competition/server.py`의 것 또는 팀 라우터)를 함께 붙인다. 같은 프로세스로 둘지는 팀 선택(PRD §9 D4, 리더 결정). 가드 담당은 `/tutor`를 고치지 않는다. 터널 주소로 `/tutor`도 200인지 확인한다
- [ ] 터널 주소로 `curl -i`: 인증 성공 200, 실패 401(`/guard`·`/tutor` 둘 다), `/guard/` 리다이렉트 없음, `http`→`https` 전환 없음
- [ ] 로컬 채점기: `python3 harness/score_local.py --guard-url …` 실행 → S1.4 대조
- [ ] `submission.json`(`token`, `guard_url`, `tutor_url` = 이 런박스의 `…/proxy/8000/tutor`, `commit`) 채우고 제출. 채점이 끝날 때까지 탭 유지
- [ ] 런박스 재시작 절차(세션 2일 제한)를 §4에 적는다

**수용 기준**: 채점 메시지의 시간 초과·형식 오류 0, 가드 점수 기록(§4). 커밋 해시가 `submission.json`에 있다(GR-14).

### S1.6 런박스 실측 (nano 지연·파라미터)

| 항목 | 내용 |
|---|---|
| GR / ADR | GR-10, PRD §11-2-1 · ADR-0011 |
| 우선 · 예상 · 모델 | P0 · 2h · 본인(코드 초안은 Sonnet) |
| 선행 | S1.3, S1.5 |
| 브랜치 | `gr/s1.6-probe` |

**만들 파일**: `src/guardrail/eval/probe.py`, `tests/test_probe.py`(FakeLLM으로 집계만).

**작업**
- `probe.py --calls 200`: ① 파라미터 수용(`reasoning_effort=low`, `verbosity=low`, json_schema strict, `max_completion_tokens=800`) 각각 따로 1회 ② `max_completion_tokens` 800 대 1200–1600에서 `finish_reason=length` 비율(같은 `InputOut`·`OutputOut` 프롬프트), 길이 잘림이 보이면 큰 값으로 `default.yaml`을 바꾸는 PR ③ `InputOut` 크기의 프롬프트로 동시 1·16·64에서 지연 p50·p95, 오류 코드 분포, 출력 토큰 ④ 총 호출 ≤200, 시작 전에 예상 호출 수를 찍는다.
- 결과를 §4 표에 적는다. json_schema가 안 되면 `default.yaml`의 `structured_output`을 바꾸는 PR을 함께 낸다.

- 게이트 크기(`llm.gate`, 기본 64)는 이 실측 뒤에 정한다. 가드는 요청마다 2회를 병렬로 부르고, 같은 프로세스면 튜터와 게이트를 함께 쓰기 때문이다.

**수용 기준**: §4 실측 표가 채워진다. p95가 12초 읽기 한도에 가까우면 `budget_s`·`timeout_s`를 다시 정하자는 메모를 master-plan §8에 올린다.

## 4. Phase 끝 점검과 결과

- [ ] 관문 ①–④ 판정, [master-plan §4](../master-plan.md#4-공통-계약)와 코드(서명·설정 키·CLI) 15분 대조
- [ ] C1 에픽 체크리스트 갱신, C2 이슈 생성

| 항목 | 결과(실측 후 채움) |
|---|---|
| 런박스 Python·설치 방법 | |
| 첫 제출: 가드 점수·시간 초과·형식 오류 | |
| `score.py` vs `score_local.py` 가드 점수 차 | |
| 파라미터 수용(effort low·verbosity·json_schema strict·max_completion_tokens, 각각 따로) | |
| `max_completion_tokens` 800 대 1200–1600 `finish_reason=length` 비율 | |
| nano 지연 p50/p95 (동시 1·16·64) | |
| 재시작 절차 | |
