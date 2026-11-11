# integration

가드(`/guard`)와 튜터(`/tutor`) **사이**: 계약 타입 · 범주→action 표 · 결정적 수정 · 루트 `app.py` 조립 · 하네스(총점 · ablation · recall) · 부하 · 레드팀 · 제출 · CI. 담당 통합 · 평가.

## 기준 문서
- 대회 규격(최우선): `competition/SPEC.md` · 채점 코드: `competition/grader/scoring.py` · `judge.py`, `competition/harness/score_local.py`
- 통합 요구사항: `integration/docs/PRD.md`(IN-01–IN-12, 결정 I1–I13, 미결 §10)
- 세부 플랜: `integration/docs/plan/S1.md`–`S4.md`(슬라이스 I1.1… · 테스트 먼저 · 수용 기준). **지금은 S1.**
- 맞닿는 문서: `tutor/docs/TUTOR_PRD.md`(TU-xx) · `tutor/CLAUDE.md`, `guardrail/docs/PRD.md` · `guardrail/docs/interface.md` · `guardrail/CLAUDE.md`, `docs/PRD.md`(팀 기획서, 관문 M1–M5)
- 문서끼리 다르면 SPEC → 채점 코드 → 각 트랙 PRD 순으로 따른다. 다른 걸 발견하면 고치지 말고 먼저 알린다.

## 소유 범위
- **만든다**: `integration/` · `harness/` · `config/` · `tests/integration/` · 루트 `app.py` · 루트 `pyproject.toml`(uv workspace) · `.github/workflows/integration-ci.yml` · `docs/PRD.md`
- **건드리지 않는다**: `guardrail/**`(가드), `tutor/**`(튜터), `competition/**`(배포 원본). 필요한 변경은 해당 PR · 이슈에 사실 + 요청으로 남긴다.
- **import만 한다**: 가드 `guardrail.server.create_app` · `guardrail.schema.Category` · `decision_of` · `guardrail.eval.cv.load_splits` · `train_rows` · `guardrail.eval.score`, 튜터 `tutor.router` · `tutor.TUTOR_DEFAULT` · `tutor.startup` · `retrieve()`.

## 합의된 구조 (PRD §3 · IN-05, 가드 #92)
- 루트 `app.py`는 한 줄: `create_app(make_guard, tutor_router=tutor.router, defaults={"/tutor": tutor.TUTOR_DEFAULT}, startups=[tutor.startup])`. 인증 · 메타 로그 · 예외→200 · lifespan은 가드 골격.
- `/tutor` 흐름: `retrieve ∥ classify(가드) ∥ 주 호출` → 합치기(classify가 공격 4범주인데 주 호출 action이 표와 다르면 표 템플릿) → `judge_output(category=)` → **결정적 수정**(IN-04). v1은 이 병렬안(I13 ⓒ), 동결값은 11/18 측정.
- 주입된 `/tutor`에는 가드의 `budget_s + 5` 안전망과 4MiB 본문 상한이 걸리지 않는다. 튜터 라우터가 직접 걸고, 통합 계약 테스트가 확인한다.
- 설정은 루트 `config/` 한 곳: `actions.yaml`(통합) · `runtime.yaml`(`judge_skip_s` · `fallback_action`) · `tutor.yaml`(튜터). `budget_s`는 `request.app.state.cfg.budget_s`(가드 로드값)를 읽는다.

## 계약 (IN-01 · IN-03 · IN-04)
- `integration/contracts.py`: `Section{id, subject, title, text, tables, names}`(`tables` · `names`는 기본 빈 목록, PRD IN-01이 원본) · `Evidence{sid, quote}`(`sid` = `Section.id`) · `Sentence{text, kind: claim|non_claim, evidence}` · `TutorResult{answer, action, citations}`. action 7종: `answer` · `scaffold` · `clarify` · `abstain` · `redirect` · `refuse` · `escalate`.
- `config/actions.yaml`이 범주→action 표의 **유일한 원본**. 튜터는 import만 한다. 표에는 범주 · action · 생성 여부 · 인용 규칙만 둔다 — 고정 문장(템플릿) 문구와 그 키는 튜터 소유라 여기 두지 않고, 튜터가 `(범주, action)`으로 찾는다. 공개 세트 200/200과 일치해야 한다(harmful은 refuse / redirect 두 갈래).
- 결정적 수정 `finalize()` 순서: quote ⊆ 섹션 원문(`scoring.normalize` 기준) 검사 → `unsupported` 구간이 든 문장 삭제 → `safe=false`면 폐기 + refuse 템플릿 → 남은 claim 0이어도 **action 유지**(abstain 전환 금지) → citations ≤ `max_citations`(기본 1) · 정규식 `^(PY|PR|FIN)-\d{2}-\d{2}$` · 실존 48개 → `answer` 4,000 유니코드 문자.
- `answer` · `scaffold`(answer_seeking)는 citations를 항상 1개 채운다(남은 claim의 sid, 없으면 검색 1위). abstain · academic scaffold는 템플릿이 자료를 말했으면 그 섹션, 아니면 `[]`.
- 판정 생략(시간 부족) · 판정 실패 → 생성된 답과 action 그대로. 생성 실패 기본 action은 `fallback_action`(튜터 TU-12가 주인).

## 채점에서 통합이 기억할 것 (`scoring.py`)
- 총점 = 0.5 × 튜터 + 0.5 × 가드. 제출당 ≈600요청(각 ≈300), 64동시, 요청당 30초, 기준 채점 ≈2분.
- 인용 F1은 정답 근거가 있는 문항만 채점(74행). 근거 충실은 **우리가 인용한 섹션 원문만** 근거로 본다(83행) → 과목 사실을 말하면서 인용을 비우면 전부 근거 없음.
- `span_f1`은 문자 다중집합 F1. 판정 실패가 튜터 문항의 5%를 넘으면 실패 문항 전부 0점.
- 가드 `eval/score.score_responses`는 **가드 200행만** 채점한다. 튜터 채점은 하네스가 `scoring.score_tutor` + `score_local.as_scoring_input`을 직접 부른다.
- 분할은 가드 `guardrail/src/guardrail/eval/splits/v1.json` **하나**를 두 트랙이 같이 쓴다(홀드아웃 가드 40 + 튜터 40, 폴드마다 32 + 32, `load_splits` · `train_rows(split, holdout=True)`). 따로 분할 파일을 만들지 않는다 — 가드 kNN · LR이 튜터 행도 학습해서 분할이 둘이면 홀드아웃이 샌다. 홀드아웃 실행은 동결 전 최대 3회.

## 명령 (저장소 루트에서)
- `uv sync --locked` · `uv run pytest tests/integration` · `uv run ruff check .` · `uv run ruff format .`
- 루트 `pyproject.toml`(workspace)이 생기기 전에는 `python -m pytest tests/integration`으로 돌린다. Python 3.12 기준(3.13 전용 기능 금지).
- 테스트 폴더는 `tests/__init__.py` + `tests/integration/__init__.py`를 둘 다 둔다. `tests/__init__.py`가 없으면 pytest가 `tests/`를 경로 앞에 넣어 `import integration`이 테스트 폴더를 가리킨다(튜터 `tests/tutor`도 같은 이유).
- 하네스: `uv run python -m harness.run_public --preset v1_parallel`(실행 전 예상 호출 수를 출력하고 상한을 넘으면 멈춘다). 실제 API 부하(600요청 · 64동시)는 대회 기간 2회만.

## 작업 방식
- 슬라이스 단위로 한다: plan 문서의 **테스트 먼저** 목록을 실패하는 테스트로 쓰고 → 구현 → 수용 기준 체크. 한 슬라이스 = 한 PR.
- **슬라이스 = 브랜치 = PR = 이슈**(가드 · 튜터와 같은 규칙). 브랜치 `int/i<스프린트>.<번호>-<이름>`(예: I1.2 → `int/i1.2-contracts`, I1.3 → `int/i1.3-finalize`) — 가드 `gr/s1.2-…`, 튜터 `tutor/t1.1-…`, 문서 `docs/…`와 같은 모양. base `main`(앞 슬라이스가 머지 전이면 그 브랜치 위에 쌓고, 머지되면 base를 `main`으로 바꾼다). PR 본문에 `Closes #<이슈>`, 라벨 `integration` + `integration:P0|P1` + `sprint:S1`, 마일스톤 M1–M5.
- 머지 전 네 검사 green: `guardrail / gate` · `integration / gate` · `pr-policy` · `integration / secret scan (repo)`.
- 수치를 문서 · PR에 적을 때는 공개 세트나 코드로 다시 계산한 값만 쓴다. 추정은 `[측정]`으로 표시한다.

## 대회 규칙 (어기면 실격)
- 채팅 `gpt-5-nano` · 임베딩 `text-embedding-3-small`만. 모델 파일(임베딩 · 리랭커 · 분류), 외부 API, 인터넷 사전 · 데이터셋, 다른 LLM이 만든 데이터 묶음을 서버에 넣지 않는다. 출처는 `docs/provenance.md`.
- integration-ci가 막는 것: 금지 의존성(torch · transformers · onnxruntime · faiss · tiktoken · huggingface-hub 등, 보수 목록), 코드 안 URL 리터럴(주소는 환경변수로만), 본문 로깅, `.env` · 실제 토큰이 든 `submission.json`.
- **서버 로그는 시각 · 경로 · 길이 · 상태 · ms · 오류 클래스만.** 요청 · 답변 본문, 예외 메시지, 트레이스백을 남기지 않는다. `print` 금지. 길이만 찍을 때도 본문 변수는 `len()` 안에만 둔다.
- 상태 없음: 요청 사이에 값을 저장 · 재사용하지 않는다(캐시 금지). 질문→답 표를 코드에 넣지 않는다(하드코딩 금지). `session_id` · 헤더는 인증 외에 쓰지 않는다. 요청 처리는 전부 런박스 안에서.
- API 키 · `TEAM_TOKEN`은 환경변수로만. `.env`를 커밋하지 않는다. `competition/submission.json`의 토큰은 양식 값(`my-token`) 그대로 둔다.
- 커밋 메시지에 `Co-Authored-By:` 줄, PR 제목 · 본문에 `🤖 Generated with Claude Code` 줄을 넣지 않는다(`pr-policy / attribution`이 PR을 막는다. 설정은 저장소 `.claude/settings.json`의 `attribution`).
- 문서 · 커밋 · PR 본문에 담당자 실명을 쓰지 않는다. "통합 · 평가 담당"으로 쓴다.
