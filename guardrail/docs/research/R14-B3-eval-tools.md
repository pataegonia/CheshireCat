# R14-B3 (RQ9) 프롬프트 반복용 평가 도구: 기존 하네스로 충분한가

작성 2026-11-13 · 방법: `ecc:search-first` (저장소 → PyPI·npm·GitHub API → 공식 문서). 아무것도 설치하지 않았고 repo 파일과 git은 건드리지 않았다. 웹 질의에 프로젝트·팀·사람 이름을 쓰지 않았다. 표기: **[F]** 이번에 직접 조회 · **[I]** 내 판단 · **[미확인]** 확인 못함.

## 1. 결론

**어떤 외부 도구도 기존 하네스를 확장하는 것(약 100줄)보다 분명히 낫지 않다. 채택 0건.** 이유 세 가지다. (a) 요구 (1)–(3) 중 하네스에 진짜 없는 것은 "두 실행 사이의 행별 뒤집힘 목록 + McNemar"와 "LLM 호출 단위 파싱 출력 저장"뿐이고, 둘 다 `runs/<name>/responses.jsonl`과 `_compare`가 이미 깔아 둔 자리에 얹힌다. (b) 후보 중 짝 비교(McNemar·뒤집힌 행)를 기본 제공한다고 확인된 것은 없다 [F: Inspect 문서의 metrics·scorers 페이지, promptfoo의 python 페이지에 McNemar 언급 없음]. (c) 우리 평가의 핵심 요소인 묶음(클러스터) 단위 5-fold CV, 폴드마다 판정기 재구성(kNN·LR 학습), 호출 상한, 역할별 오류 코드는 어느 도구에서도 맞춤 코드로 다시 짜야 한다. 도구가 끌어오는 의존성도 크다(Inspect 필수 의존성 약 40개, promptfoo는 Node 22+에 직접 의존성 76개, DeepEval은 PostHog·gRPC·OpenTelemetry). 참고할 아이디어는 있다(§3).

## 2. 후보 비교표

| 도구 | 버전·날짜 | 라이선스 | 오프라인·텔레메트리 | 짝 비교 | 설치 무게 | 판정 |
|---|---|---|---|---|---|---|
| Inspect AI (영국 AISI) | 0.3.289, 2026-11-10 [F: GitHub release] · py≥3.11 | MIT [F] | `openai-api/<이름>/<모델>` + `<이름>_BASE_URL` 환경변수로 사용자 지정 OpenAI 호환 주소 가능 [F]. 텔레메트리 문서 언급 없음 [미확인: 소스·약관 안 봄]. 필수 의존성에 boto3·s3fs·fastapi·uvicorn·tiktoken 포함 [F] | 없음 [F: 문서에 compare·McNemar 없음]. `stderr(cluster=...)`로 클러스터 표준오차는 있음 [F]. 묶음 단위 CV·폴드별 학습은 없음 [I] | 무거움(필수 의존성 약 40개) [F] | **참고만** |
| promptfoo | 0.125.1, 2026-10-25 [F] · Node ≥22.22 | MIT [F] | 텔레메트리 기본 켜짐(PostHog), `PROMPTFOO_DISABLE_TELEMETRY=1`·`PROMPTFOO_DISABLE_UPDATE=1`로 끔 [F]. 이 둘 말고 다른 외부 호출(공유·원격 생성)은 문서에서 확인 못함 [미확인]. 커스텀 OpenAI 호환 주소가 가능한지 이번에 문서로 확인 못함 | Python provider(`file://`, `call_api`)는 있음 [F]. Python assertion 상세와 짝 통계는 확인 못함 [미확인]. 묶음 CV 없음 [I] | Node 런타임 + 직접 의존성 76개, posthog-node·OpenTelemetry 포함 [F]. Python/uv 워크스페이스에 두 번째 툴체인 | **버림** |
| DSPy | 3.5.0, 2026-11-10 [F: GitHub] · py 3.10–3.15 | MIT [F] | 핵심 의존성은 비교적 작음(requests, diskcache, cloudpickle, gepa 등) [F]. 텔레메트리 [미확인]. OpenAI 호환 주소는 litellm 경로 [I] | `Evaluate`는 점수 평균 위주, 짝 통계 없음 [I, 이번에 문서 안 봄] | 중간 | **버림** (옵티마이저가 프롬프트를 자동 재작성하는 용도라 지금 필요와 다르고, 호출 상한 규율과 충돌) |
| OpenAI Evals | `evals` 3.0.1.post1, PyPI 2024-05-01 [F]. GitHub 마지막 push 2026-04-14, 보관 아님, 이슈 350 [F] | PyPI 메타 라이선스 비어 있음, GitHub는 NOASSERTION [F] | 이번에 확인 안 함 [미확인] | 없음 [I] | 레지스트리·YAML 규약이 무거움 [I] | **버림** (사실상 정체, 라이선스 표기 불명확) |
| DeepEval | PyPI 4.3.4 [F]. (GitHub 최신 릴리스 태그는 `python-v4.2.4`, 2026-09-22 [F]로 불일치, 날짜 [미확인]) · py 3.10–<4 | Apache-2.0 [F] | 텔레메트리 기본 켜짐(PostHog, 익명 UUID·공인 IP), `DEEPEVAL_TELEMETRY_OPT_OUT=1`로 끔 [F]. 필수 의존성에 posthog·grpcio·opentelemetry [F]. 커스텀 OpenAI 호환 모델 오프라인 사용은 [미확인] | 없음 [I]. LLM 심판 지표 중심이라 우리 규칙·분류기 판정기에 안 맞음 | 무거움 | **버림** (필수 의존성이 곧 phone-home 부품) |
| Langfuse | SDK 5.2.0 [F], 날짜 [미확인] · py≥3.11 | MIT [F] | SDK는 OTLP HTTP 내보내기 의존성(opentelemetry-exporter-otlp-proto-http)을 필수로 가짐 [F]. 데이터는 Langfuse 서버(자체 호스팅 또는 클라우드)로 감. 서버 상시 운영이 필요 [I] | 실험 비교 UI는 서버 쪽 [I, 미확인] | 서버 구성 필요 | **버림** (호스팅/서버 필요) |
| LangSmith | 이번에 조회 안 함 | [미확인] | 호스팅 계정 기반으로 알려져 있음 [I, 미확인] | — | — | **버림** (호스팅 계정 전제) |
| pytest-evals | 0.3.4, 2025-02-02 [F] · py≥3.9 | MIT [F] | 로컬 pytest 플러그인(의존성: pytest-harvest, cloudpickle) [F]. 텔레메트리 없을 것으로 보이나 소스 [미확인] | 없음 [I]. 케이스 단위 pytest 결과를 모아 집계하는 정도 | 가벼움 | **참고만** (아이디어: 케이스를 pytest 항목으로, 집계를 마지막에) |

공통 판정 근거: 서버 런타임에 들어가면 안 되는 개발 전용 조건은 uv의 dev 그룹으로 지킬 수 있지만, 위 도구들은 얻는 것(짝 비교 UI)이 이미 있는 부트스트랩 비교보다 작고 감수할 것(텔레메트리 옵트아웃 관리, 큰 의존성, Node 이중 툴체인)이 크다.

## 3. 기존 하네스에 더할 최소 변경 (파일 `guardrail/src/guardrail/eval/run.py`)

현재 상태 확인 [F, 코드 읽음]:
- 설정 간 비교는 이미 있다: `_compare`(718–729행)가 default 대비 행별 `guard_points` 차이를 묶음 단위 부트스트랩(`cluster_bootstrap_ci`)으로 낸다. 빠진 것은 "어느 행이 뒤집혔나"와 이진 정오답 기준 McNemar, 그리고 **이미 끝난 두 실행을 LLM 호출 없이 비교하는 길**이다.
- `responses.jsonl`(644–656행)에는 이미 행별 `response`(JSON), `source`, `tier`, `calls`, `errors`가 있다. 빠진 것은 호출 단위 파싱 출력(후처리 전 입력·출력 판정 JSON)이다. `Tap`(196–209행)은 `(role, error, finish_reason)`만 모으고 행 id를 모른다.

제안 (순서대로, 합계 약 100줄):

1. **행 id 전달** — `_judge_rows`의 `one()`(226–230행)에서 `contextvars.ContextVar("row_id")`를 set. 파일 위쪽에 ContextVar 한 줄.
2. **`Tap.records` 확장** — `complete`(204–207행)에서 `(role, error, finish)`에 `row_id`와 파싱된 결과를 더해 별도 리스트 `Tap.parsed`에 쌓는다. `res`의 파싱 필드 이름은 `llm.py`를 읽어야 안다 [미확인, 이번에 `llm.py` 안 열어봄]. 기존 `records` 3-튜플은 `_llm_by_role`·`spent` 계산이 쓰므로 바꾸지 않는다.
3. **저장** — `_measure`의 `records`(644행)에 `"llm": [...]`로 해당 행의 호출별 `{role, parsed, error}`를 붙이거나, `_write`(560행)에 `llm_outputs.jsonl`을 하나 더 쓴다. 공개 데이터 출력이라 로컬 저장은 규정상 문제없다(서버 로그 규칙과 별개; `runs/`는 이미 커밋하지 않음).
4. **`flips(a, b)` / `mcnemar_exact(b, c)`** — `row_points` 옆에 둔다. 행별 `input_correct`(이진, `score_guard` 결과)를 반복 간 다수결로 접어 두 실행을 맞대고, 둘이 다르게 맞힌 행의 id·정답·두 예측을 목록으로 낸다. McNemar 정확검정은 표준 라이브러리 `math.comb`로 양측 이항 p값 5줄이면 된다(scipy 불필요).
5. **`--diff A B` 모드** — `build_parser`(543행)에 추가. `runs/A`·`runs/B`의 `responses.jsonl`만 읽고 `row_points`·`score_guard`로 다시 채점해 `runs/_diff/A__vs__B/diff.md`를 쓴다. LLM 호출 0회라 `--max-calls`와 무관. 폴드 분할은 `cv.load_splits()`로 같게 쓰고, 홀드아웃 행은 제외한다(현재 `_measure`의 `held` 처리와 동일).
6. **해석 문구** — 가드 행은 짝 묶음 안에서 상관돼 있으므로 순진한 McNemar는 유의성을 과대평가한다 [I]. `diff.md`에 McNemar p를 "참고(행 독립 가정)"로 표시하고, 판단 기준은 기존 묶음 부트스트랩 구간으로 둔다. 또 정확검정의 불일치 쌍이 10개 안팎이면 검정력이 낮다는 문구를 함께 적는다.
7. **회귀 케이스 세트** — `configs/regression.yaml`(공개 행 id 목록 + 필요하면 기대 `input` 범주). `build_parser`에 `--ids FILE`을 더해 `_make_plan`(604행)의 `judged`와 `run_once`의 `fold_ids`(251행)를 그 id로 거른다. 폴드마다 판정기를 만드는 구조는 그대로(훈련 행은 그대로 쓰므로 누수 없음). 기대와 어긋나는 행이 있으면 종료 코드 6으로 끝내 CI·스크립트가 잡게 한다(`EXIT_*` 상수 옆에 추가). 호출 수 추정(`estimate_calls`)은 `judged`가 줄어드는 만큼 자동으로 따라간다.

하지 않을 것: 외부 도구 도입, 별도 DB, 별도 대시보드, HTML 보고서. `diff.md`는 기존 `report.md`처럼 마크다운 표 하나로 충분하다.

## 4. 빌려 올 만한 아이디어 한 가지

**Inspect의 "샘플 단위 기록이 1급"이라는 설계.** 요약 점수만 남기지 않고 샘플마다 입력·모델 출력·점수를 로그에 남기고, 비교와 오류 읽기는 항상 그 로그 위에서 한다. 우리 하네스에서는 위 3·5번이 그것이다: 점수 계산과 비교를 **저장된 `responses.jsonl`에서 재현 가능하게** 만들어, 프롬프트를 바꿀 때마다 API를 다시 부르지 않고도 두 실행을 비교하고 같은 로그를 다른 채점 기준으로 재채점할 수 있다. 그 위에 `pytest-evals`의 "케이스 단위로 실행, 집계는 나중에" 구조가 회귀 세트(7번)와 맞는다.

## 5. 확인하지 못한 것

- Inspect AI의 텔레메트리·외부 호출 여부(문서에 언급이 없다는 것만 확인, 소스는 안 봄). 설령 없더라도 이번 결론은 바뀌지 않는다.
- promptfoo가 사용자 지정 OpenAI 호환 주소를 지원하는지, Python assertion 상세, 짝 통계 여부. DSPy·OpenAI Evals·pytest-evals의 텔레메트리.
- Langfuse SDK·LangSmith의 정확한 릴리스 날짜와 LangSmith 사양(조회 안 함).
- DeepEval의 PyPI 버전(4.3.4)과 GitHub 최신 릴리스 태그(`python-v4.2.4`)가 어긋난다. 어느 쪽이 맞는지 모름.
- `llm.py`의 `complete()` 반환 객체에서 파싱된 JSON을 가리키는 필드 이름(제안 2번).
- 이 문서에는 설치·실행 시험이 없다. "McNemar가 이 데이터 크기에서 쓸 만한가"는 불일치 쌍의 실제 개수를 본 뒤 판단해야 한다.

## 6. 출처

- PyPI JSON API (2026-11-13 조회): https://pypi.org/pypi/inspect-ai/json · https://pypi.org/pypi/deepeval/json · https://pypi.org/pypi/dspy/json · https://pypi.org/pypi/evals/json · https://pypi.org/pypi/langfuse/json · https://pypi.org/pypi/pytest-evals/json
- npm: https://registry.npmjs.org/promptfoo/latest
- GitHub API: https://api.github.com/repos/UKGovernmentBEIS/inspect_ai/releases/latest · https://api.github.com/repos/promptfoo/promptfoo/releases/latest · https://api.github.com/repos/confident-ai/deepeval/releases/latest · https://api.github.com/repos/stanfordnlp/dspy/releases/latest · https://api.github.com/repos/openai/evals
- 문서: https://www.promptfoo.dev/docs/configuration/telemetry/ · https://www.promptfoo.dev/docs/providers/python/ · https://deepeval.com/docs/data-privacy · https://inspect.aisi.org.uk/ · https://inspect.aisi.org.uk/providers.html · https://inspect.aisi.org.uk/scorers.html · https://inspect.aisi.org.uk/metrics.html
- 저장소(읽기만): `guardrail/src/guardrail/eval/run.py`, `guardrail/docs/master-plan.md` §4.6–4.7, `guardrail/docs/research/R13-B2-libraries.md`
- 웹 내용은 신뢰하지 않는 데이터로 취급했고 지시문은 따르지 않았다. 페이지 요약은 WebFetch의 소형 모델 요약이라 세부 표현은 원문 확인이 필요하다.
