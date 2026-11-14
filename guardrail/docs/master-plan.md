# 가드레일 master-plan

작성 2026-10-30 · 갱신 2026-11-10 · 담당 danielcho02 · 상태: **v23** (2026-11-10, PRD v6 전환). v1–v22: 커밋 `50d6142` §13
근거: [PRD.md](PRD.md) v6(GR-01–GR-14) · [adr/](adr/README.md)(0012–0014, 각 ADR의 v6 수정 블록) · [R13](research/R13-design-research.md)(결정 D1–D10) · [SPEC](../../competition/SPEC.md) · [scoring.py](../../competition/grader/scoring.py) · [skills.md](skills.md)
세부 플랜: [plan/C1](plan/C1-foundation.md) · [C2](plan/C2-judges.md) · [C3](plan/C3-tuning.md) · [C4](plan/C4-wrapup.md) (GP0–GP4는 v4 기록)

이 문서는 PRD v6의 구성(PRD §4)을 코드로 옮기는 큰 줄기다. 무엇을 만들지는 PRD가, 왜 그렇게 하는지는 ADR이 정한다. 이 문서는 **순서·모듈·계약**을 정한다.
슬라이스 하나가 브랜치 하나, PR 하나, 이슈 하나다. Phase 문서와 이 문서가 다르면 이 문서가 맞고 Phase 문서를 고친다.

---

## 1. 범위

| 포함 | 제외 |
|---|---|
| `POST /guard` 판정기: 라우터, 전처리, 신호·규칙, kNN·LR, KB 검색, nano 판정 2개, 조립 | `/tutor` 구현(검색·생성·action) |
| 공개 세트 평가 하네스(짝 묶음 교차 검증, 채점 코드 import), 부하·실측 도구 | 팀 서버 통합 주인, 토큰·제출 계정 운영(리더 몫) |
| 런박스 배포 절차, 가드 쪽 제출 확인, 시연용 가드 수치·로그 코드·provenance | 시연 발표 형식 |
| `/tutor`와 공유할 부분의 **제안**과 import할 수 있는 함수(PRD §9) | 다른 팀원의 코드 수정 |

팀과 맞닿는 결정은 PRD §9에 제안으로만 둔다. 이 문서 §9는 정보다.

## 2. 일정과 Phase 관문

마감 2026-11-25(수) 09:00 KST, 시연 같은 날 13:00(SPEC §9). 오늘은 11/10(화)이다. **11/20(금) 18:00 기능 동결.**

| Phase | 기간 | 보이는 변화 | 넘어가는 조건 |
|---|---|---|---|
| C0 문서 | 11/10(화) | PRD v6, ADR-0012–0014, 이 문서 v23, C1–C4 | 문서 PR 머지 |
| [C1 기반·첫 제출](plan/C1-foundation.md) | 11/11(수)–11/12(목) 오전 | v4 코드 정리, 서버 골격(인증·200 보장·로그 규칙), 스키마, 채점 재현, 짝 묶음 분할, **상수 응답 첫 제출**, 런박스 실측 | 터널 주소로 200 JSON·3xx 0·형식 오류 0, 채점 재현 차 ≤0.005(같은 상수 응답 기준), 실측 표(PRD §11-2-1) |
| [C2 판정 v1](plan/C2-judges.md) | 11/12(목) 오후–11/15(일) | 전처리·KB·신호·규칙·kNN·LR·nano 판정 2개·조립, 하네스 v1, 부하 시험, **v1 제출** | 교차 검증 가드 점수가 nano 단독 베이스라인보다 높고 3회 분산 밖, 동시 64에서 시간 초과 0 |
| [C3 조정](plan/C3-tuning.md) | 11/16(월)–11/20(금) | 측정 기반 켜기 결정, 입력 프롬프트 조정, ablation, 홀드아웃 | 홀드아웃 결과·ablation 표 확정, 동결 커밋 SHA 기록 |
| [C4 마무리](plan/C4-wrapup.md) | 11/21(토)–11/24(화) | 규칙 준수 점검, 시연 자료, **최종 제출** | 제출 체크리스트 통과, 최종 제출 11/24 18:00까지 |

- **계정별 마지막 제출**이 그 계정 점수이고, 팀 점수는 계정 중 최고다(SPEC §9). 가드 쪽 제출은 Phase 관문마다 한 번(C1 상수, C2 v1, C3 동결본, C4 최종)만 한다. 제출 하나가 약 600요청이라 잦은 제출을 피한다. 이것은 팀 정책이고 SPEC보다 보수적이다(SPEC §9는 제출 횟수를 제한하지 않는다. 과한 호출 경고는 SPEC §7.1).
- 제출 점수는 표본이 바뀌어 흔들린다. 판단은 로컬 짝 묶음 교차 검증과 3회 분산으로 한다(R13 D10).
- 마감 직전에는 검증된 커밋만 낸다. 11/25 아침에는 제출하지 않는다(서버 상태 확인만).

**용량**

| Phase | 일수 | 슬라이스 합계 | 그중 P0 | 병렬로 돌릴 몫(Sonnet 서브에이전트) |
|---|---|---|---|---|
| C1 | 1.5 | 20h | 20h | S1.3·S1.4(8h) |
| C2 | 3.5 | 50h | 48h | S2.1·S2.2·S2.5·S2.9(18h) + S2.11(2h) = 20h |
| C3 | 5 | 30h | 15h | S3.3–S3.6 실행(15h) |
| C4 | 4 | 11h | 11h | S4.3 초안(2h) |
| 합계 | 14 | **111h** | **94h** | 약 45h |

가용 시간은 11/11–11/24 14일, 하루 약 9h로 본인 약 126h다. 병렬 몫을 빼면 본인 일은 약 66h라 여유가 있지만 **C2가 몰린다**(3.5일에 본인 30h). C2가 11/15 밤까지 관문을 못 넘으면 §7 순서대로 자르고 C3 앞 하루를 쓴다. 동결일(11/20)은 옮기지 않는다. S1.6·S2.10은 본인이 실행하고 코드 초안만 Sonnet에 맡기므로 위 병렬 몫에 넣지 않았다. 본인 시간 합계는 그대로다.

## 3. 아키텍처 → 코드

PRD §4의 단계가 모듈 하나씩에 대응한다.

| PRD §4 단계 | 모듈 | 주요 함수 | GR | ADR |
|---|---|---|---|---|
| 인증·요청 읽기·200 보장·로그 | `server.py`, `schema.py` | `create_app`, `router`, `setup_guard`, `guard_lifespan`, `parse_request` | 01·09·12 | 0009 |
| ① 전처리 | `preprocess.py` | `normalize`, `reveal_hidden`, `decode_tags`, `strip_tags` | 02 | 0001 |
| ② 결정적 신호 | `signals.py` | `is_low_signal`, `is_refusal`, `has_hidden_tags` | 06 | 0014 |
| ② 숫자·리터럴 | `rules.py` | `extract_literals`, `literal_rule` | 06·07 | 0014 |
| ③ 보조 판정기 | `knn.py`, `classifier.py`, `clusters.py` | `KnnIndex`, `LrClassifier`, `build_clusters`(짝 묶음. `KnnIndex`가 기동 때 직접 계산하고 `eval.cv`도 import. 서버는 eval 패키지에 의존하지 않는다) | 05 | 0013 |
| ④ KB 검색(R13-A가 잰 BM25 변형) | `kb.py` | `KbIndex.load`, `KbIndex.rank`(→ `Ranking`), `KbIndex.search` | 03 | 0010 |
| ⑤ 입력 판정 | `judges/input_judge.py` + `prompts/input_judge.md` | `judge_input` | 04 | 0012·0013 |
| ⑥ 출력 근거 판정 | `judges/output_judge.py` + `prompts/output_judge.md` | `ask_grounded` | 07 | 0014 |
| ⑦–⑨ decision·3단·safe·기본값 | `decide.py`(+ `schema.decision_of`) | `output_verdict`, `safe_of`, `fallback_category` | 07·08·09 | 0012·0014 |
| 조율(병렬 2회·요청 예산) | `guard.py` | `Guard.build`, `Guard.judge`, `Guard.classify`, `Guard.judge_output(messages, candidate_answer, category=None)` | 09·10 | 0009·0011 |
| LLM 게이트·출력 스키마 | `llm.py`, `judges/schemas.py` | `LLM.start`, `LLM.complete`, `LLM.probe`, `LLM.slot`, `InputOut`·`OutputOut`·`OutputBaselineOut` | 10 | 0011 |
| 설정 | `config.py` | `load_config`(`GUARD_*` 환경변수), `config_hash`, `load_prompt` | 11 | — |
| 공개 세트 로더 | `data.py` | `load_public`, `labeled_rows` | 05·13 | 0013 |

```
guardrail/
  src/guardrail/
    server.py  schema.py  guard.py  decide.py  config.py  llm.py  data.py
    preprocess.py  signals.py  rules.py  knn.py  classifier.py  clusters.py  kb.py
    judges/  schemas.py  input_judge.py  output_judge.py
    eval/    score.py  cv.py  run.py  load.py  probe.py  stats.py  splits/v1.json  splits/holdout_views.json
  prompts/   input_judge.md  output_judge.md  output_judge_baseline.md   (팀 작성, id·version 머리말)
  configs/   default.yaml  baseline.yaml  ablation/*.yaml
  tests/
```

**기존 코드 처리**

| 기존 | 처리 | 슬라이스 |
|---|---|---|
| `config.py` | 고쳐 씀. YAML 로드·`overrides`·`config_hash`·`load_prompt`는 두고, 설정 모델을 §4.4로 바꾼다(`stages`·`policy`·`thresholds`·`tutor`·`service_log`·`faults` 삭제). `${ENV}` 치환은 지운다(v6 설정에 치환할 필드가 없다). 대신 `GUARD_CONFIG`·`GUARD_KB_DIR`·`GUARD_PUBLIC_DIR`·`GUARD_PROMPTS_DIR`를 로더가 읽는다(§4.4) | S1.1 |
| `llm.py` | S1.1에서 삭제(v4 `Guard`·`types`에 묶임), S1.3이 §4.3대로 새로 씀(v4 파일은 git 이력의 참고용). 역할 2개, AIMD 삭제(고정 세마포어), 시도 2회(재시도 1), 대기는 요청 마감 안에서만, `Retry-After`가 남은 시간을 넘으면 바로 실패, GR-10 파라미터, 기동 시험 호출, `OPENAI_*` 환경변수, `slot()` 유지, `faults`·`FAKE_OK` 삭제 | S1.3 |
| `preprocess.py` | 고쳐 씀. `normalize`(정규화본: 보이지 않는 문자 제거 → 전각·반각 접기 → 자모 조합, **NFKC 안 씀**)는 그대로 둔다. `skeleton`은 v6 서버 경로에서 쓰지 않아 지우고 `reveal_hidden`·`decode_tags`·`strip_tags`(태그 문자 U+E0000–E007F만 지움, 자모 조합 없음. 저신호 뷰)를 더한다(붙인 뷰 `joined`는 구현하지 않는다. 필요하면 ablation으로 추가) | S2.1 |
| `pipeline.py`, `trace.py`, `types.py` | 삭제. 튜터 래퍼(`Session`·`Chunk`·`Verdict`·`StageResult`·`TutorAdapter`)와 원문 trace는 v6에 없다. trace는 로그 규칙(GR-12)과 충돌 | S1.1 |
| `eval/mock_tutor.py` | 삭제. `kb.py`의 BM25는 이 파일이 아니라 `scripts/r13/common.py`의 측정 변형(토큰화 = `scoring.normalize`식: NFKC·소문자·공백·문장부호 제거 뒤 문자 2-gram)을 옮긴다(ADR-0010 v6). 조각은 `common.kb_chunks`를 그대로 옮긴다. 섹션 텍스트(md + 붙은 CSV)를 `## `로 시작하는 줄 앞에서 자른다(`re.split(r"\n(?=## )")`). 코드 블록 안의 `## ` 줄에서도 자른다(PR-02-01에 3곳). 각 조각은 자기 `## ` 소제목 줄을 포함하고, 첫 `## ` 앞의 `# 제목` 머리말은 따로 한 조각이 된다. 조각마다 문서 제목을 덧붙이지 않는다. CSV는 섹션 끝에 붙어 마지막 조각에 들어간다. 코드 블록을 자르지 않는 변형은 다시 재야 하는 ablation이다. 이 NFKC는 BM25 토큰 안에서만 쓴다 | S1.1(BM25 이식은 S2.2) |
| `eval/build_grounding.py`, `metrics.py`, `datasets.py`, `run.py` | 삭제(v4 데이터·지표). `run.py`는 S2.9가 새로 쓴다 | S1.1 |
| `eval/stats.py` | `wilson`만 남긴다. 묶음 부트스트랩은 S2.9가 더한다 | S1.1 |
| `tests/` | `test_preprocess.py` 유지·확장(골격 뷰 테스트는 S2.1이 지움), `test_config.py`는 S1.1이 다시 씀. `test_llm.py`·`fakes.py`는 S1.1이 지우고 S1.3이 새로 씀(`fakes.py`는 `FakeLLM`·`FakeClock`), 나머지 v4 테스트 삭제 | S1.1·S1.3·S2.1 |
| `configs/unguarded.yaml`, `prompts/attack_judge.md`, `resources/`, `data/seed/`, `results/`(v4) | 삭제(git 이력에 남음). v4 스모크 세트는 쓰지 않는다(PRD §8) | S1.1 |

- SPEC은 `/tutor`·`/guard`를 한 프로세스에 두라고 요구하지 않는다. 다만 각 계정의 제출은 **그 계정 런박스의** `/tutor`·`/guard` 주소여야 한다(SPEC §7). 그래서 모든 제출(C1 상수, C2 v1, 최종)에는 같은 런박스의 `/tutor`가 있어야 한다. 시작용 server.py는 둘을 한 앱에 둔다. 같은 프로세스로 둘지는 팀 선택(PRD §9 T4, 리더 결정). 가드는 APIRouter로 만들어 어느 쪽이든 붙는다. API 키는 어느 쪽이든 팀 키 하나라(SPEC §7.1) 한 프로세스를 함께 쓰면 전역 게이트가 중요하다. 가드는 `router`(FastAPI `APIRouter`)와 독립 실행용 `create_app(guard_factory)`(`guard_factory: Callable[[GuardConfig], Awaitable[Guard]]`, lifespan에서 `load_config()`로 `GUARD_CONFIG`를 읽어 부른다)를 함께 낸다. 팀 앱이 `router`만 붙이면 자기 lifespan에서 `setup_guard(app, cfg)`(async, `app.state`에 판정기·토큰·block 표를 둔다)를 부르거나 `guard_lifespan(app)`(`load_config()` → `setup_guard`)을 lifespan으로 써야 한다. `app.state.guard`가 없으면 `router`의 기동 검사가 예외를 내 서버가 뜨지 않는다(조용히 기본 응답을 내지 않는다). `create_app()`은 `/guard` 옆에 시작용 `/tutor`(`competition/server.py`의 것 또는 팀 라우터)를 함께 붙이고, `TEAM_TOKEN` Bearer 검사(`check_token`)는 가드 `APIRouter` 자체의 의존성(`APIRouter(dependencies=[Depends(check_token)])`)이라 팀 서버가 `router`만 붙여도 `/guard`는 인증된다. `create_app()`은 복사한 `/tutor` 라우트에 같은 검사를 따로 걸어 같은 401을 적용한다. 팀 서버가 준비되기 전의 제출은 `create_app()`으로 하고, `tutor_url`은 이 런박스의 `…/proxy/8000/tutor`다. 포트는 8080을 피한다(VSCode가 씀, SPEC §7).
- 서버 계약은 하나다: `Guard.judge(req) -> Judgement`(S1.2의 `ConstantGuard`도 같음). 서버는 `judgement.response.to_json(block)`만 내고, 나머지 필드는 하네스만 쓴다. 로그에 남기지 않는다.

## 4. 공통 계약

모든 슬라이스가 이 계약을 따른다. 바꿔야 하면 이 절을 먼저 고치고 PR 설명에 적는다. 응답 형식의 기준은 [contract/guard.schema.json](contract/guard.schema.json)이다.

### 4.1 타입 (`schema.py`와 모듈별 dataclass)

```python
Category = Literal["normal", "answer_seeking", "academic_dishonesty", "off_topic", "out_of_material",
                   "jailbreak_injection", "pii_disclosure", "harmful_content", "emotional_distress",
                   "low_signal_input"]
Decision = Literal["allow", "block"]
def decision_of(category: Category, block: frozenset[Category]) -> Decision   # block = 설정 block_categories(ADR-0012)

@dataclass(frozen=True)
class Message:  role: str;  content: str        # role이 user가 아니면 학습자 발화로 보지 않는다

@dataclass(frozen=True)
class GuardRequest:
    messages: tuple[Message, ...]               # 못 읽으면 ()
    candidate_answer: str | None                # 키가 없으면 None. 빈 문자열은 "". 문자열이 아닌 값도 None(출력에 한해 파싱 실패, GR-09)
    has_candidate: bool                         # 키가 있었는가(값을 못 읽어도 True) → output 필수

def parse_request(body: bytes) -> GuardRequest  # 예외 없음. 읽을 수 있는 만큼 읽는다(GR-01, GR-09)

@dataclass(frozen=True)
class OutputVerdict:  grounded: bool;  unsupported: tuple[str, ...];  safe: bool

@dataclass(frozen=True)
class GuardResponse:
    category: Category
    output: OutputVerdict | None
    def to_json(self, block: frozenset[Category]) -> dict   # input.decision = decision_of(category, block), output.category = category

@dataclass(frozen=True)
class Signals:                   # signals.py(S1.1이 dataclass를 먼저 둠)
    low_signal: bool;  refusal: bool;  hidden_tags: bool

@dataclass(frozen=True)
class LiteralToken:              # rules.py. start·end는 원문 기준
    kind: str;  value: str;  start: int;  end: int     # kind = 아래 ⓐ–ⓕ, value = scoring.normalize(토큰)

@dataclass(frozen=True)
class Hit:  section_id: str;  text: str;  score: float          # kb.py

@dataclass(frozen=True)
class Ranking:                   # kb.py. KbIndex.rank(query)가 BM25 점수를 요청당 한 번만 계산해 만든다
    hits: tuple[Hit, ...]        # 점수 내림차순 전체. 상위 k = hits[:k], 1위 섹션 = hits[0].section_id
    margin: float                # 전체 순위를 섹션별 최고 점수로 접은 뒤 1위 섹션 점수 − 다른 섹션 중 최고 점수(top_k 안만 보지 않음)
# KbIndex(kb.py): load(kb_dir), rank(query) -> Ranking,
#   search(query, k) -> list[Hit]   # = list(rank(query).hits[:k]). 섹션이 아니라 조각 상위 k개, 테스트용. judge()는 rank()를 한 번만 부른다
#   section_text(section_id) -> str   # 그 섹션의 모든 조각 + 붙은 CSV
#   chunks -> tuple[tuple[str, str], ...]   # (섹션 ID, 조각 텍스트), 섹션·조각 순서
#   scores(query) -> list[float]   # 조각 순서 BM25 점수. 튜터 섹션 순위·임베딩 색인용
#   점수 계산은 질의 2-gram → 조각 역색인으로 해도 된다. 단 점수가 common.py BM25와 1e-9 안에서 같아야 한다(테스트)

@dataclass(frozen=True)
class LabeledRow:  id: str;  text: str;  category: Category     # data.py. text = 마지막 학습자 발화의 정규화본

@dataclass(frozen=True)
class Judgement:                 # guard.py(S1.1이 dataclass를 먼저 둠). 서버는 .response만 직렬화, 로그에 남기지 않는다
    response: GuardResponse
    input_source: Literal["low_signal", "nano", "third_call", "fallback", "default"]
    output_tier: Literal[1, 2, 3, 4] | None                      # GR-07 표의 ①–④
    nano_grounded: bool | None                                  # 출력 nano 원값. v6 모드에서는 후보 답변이 있으면 늘 기록(nano_tier3와 무관, S3.1 정밀도용. `output.call_nano_when_unused: false`로 건너뛰면 None)
    answer_quote_ok: bool | None;  kb_quote_ok: bool | None      # 출력 nano의 인용이 답변·문맥의 부분 문자열인가(R14-D2). 답변 쪽은 `input.max_answer_chars`로 자른 원문(숨긴 문자를 드러내기 전)과 대조한다. 인용 원문은 여기 두지 않는다(하네스 Tap이 runs/에만 저장)
    nano_refuses: bool | None                                   # 출력 nano의 거절 판정 원값. 기록만 하고 safe에 쓰지 않는다(R14-D3)
    signals: Signals
    knn_major: Category | None;  lr_pred: Category | None
    calls: int;  errors: tuple[str, ...]                        # calls = 시작한 nano 호출 수(재시도 제외). errors = 오류 코드만
    latency_ms: Mapping[str, float]                             # 단계별(prepare·llm·total)
```

- 코드에서 `answer_quote_ok`·`kb_quote_ok`·`nano_refuses`는 기본값 `None`으로 dataclass 맨 끝에 둔다(상수 판정기와 하네스의 단독 판정기가 채우지 않아도 되게).
- `Judgement.errors`의 코드: nano 실패·마감 초과는 역할 이름(`input_judge`·`output_judge`), 호출 안의 예외는 `역할:예외종류`, 출력 쪽 준비(KB 검색·규칙 ②)나 출력 조립의 예외는 `output:예외종류`(입력 판정은 그대로 하고 범주를 살린다. 이때 grounded는 `output.failure_default`, safe는 GR-08 그대로), 판정기 전체 예외는 `guard:예외종류`, 학습자 발화 없음은 `no_user_message`. 예외 메시지는 남기지 않는다(GR-12).

- `decision`은 어디에서도 따로 저장하지 않는다. `decision_of()` 하나가 설정의 범주 → decision 표(`block_categories`)로 정한다(ADR-0012). 표 값은 공개 세트에서 잰 block 4범주다(PRD §3).
- 규칙 ②(`rules.py`)의 추출기는 `guardrail/scripts/r13/q5_ungrounded.py`의 `NUM_RX`와 비교 방식을 옮기되 세 곳을 고친 기본 변형 **V2**다(`guardrail/scripts/r13/q6_rule_fp.py`): 추출 전에 답변의 섹션 ID 인용 `\(?(?<![A-Za-z0-9])(?:PY|PR|FIN)-\d{2}-\d{2}(?![0-9])\)?`(`\b`는 숫자와 한글 사이에서 안 맞아 ASCII 경계)을 지우고, ⓕ의 글자 클래스 `[한두세네다섯]`("다 번"에도 걸림)을 `(?:한|두|세|네|다섯)`으로 바꾸고, ⓕ에 경계를 둔다(앞 글자가 한글이면 안 잡고, `개` 뒤 `념·수·발·체`("두 개인"의 `인`은 서술격 조사와 구별 안 돼 뺌), `번` 뒤 `호·역`, `배` 뒤 `열·제·치·경`이면 뺀다. 나열식이라 목록 밖 단어는 못 막는다. 공개 답변·KB에서 사라지는 매칭은 오탐 5건뿐). 토큰 종류: ⓐ 숫자(`.`·`,` 자릿수 허용) + 공백 0–1개 + 단위 `만 원·천 원·원·%·세·개월·점·번·개·씩·배·단계·권·명·회·줄` ⓑ `.`·`,`가 든 숫자(1.3, 1,000) ⓒ 두 자리 이상 수 ⓓ 숫자 리스트 리터럴(`[3, 7, 12]`) ⓔ 영문·코드 리터럴(`[A-Za-z_][A-Za-z_0-9.()]*`, 공백으로 이어진 영문 단어까지 한 토큰) ⓕ 한글 수사 `한·두·세·네·다섯` + 공백 0–1개 + `번·개·단계·가지·배`. 비교: 토큰의 `scoring.normalize` 값(NFKC·소문자·공백·문장부호 제거)이 BM25 1위 섹션 전체(`section_text`)를 같은 식으로 정규화한 문자열에 부분 문자열로 없으면 걸린다. 값 정규화(콤마·만·천·억), 단위 주·일·년·글자, 단위 없는 한글 수사, 백틱 안 영문만 보기는 기본이 아니고 ablation `rule_extra_tokens`다. 실측(V2, BM25 1위 섹션): U(가드 근거 없음) 18/35, G(가드 근거 있음) 0/32, T(튜터 참조 답변 answer·scaffold·abstain 중 evidence 있는 119행) 6/119. 점수 차 3.5 게이트면 16/35, 0/32, 3/119([R13-A §6](research/R13-public-set-analysis.md#6-규칙-②-오탐-재측정-santa-4라운드))(표본 안 값: NUM_RX 토큰 종류·V2 수정·margin 3.5가 이 공개 행을 보고 정해졌다. 비공개 검출률·정밀도는 더 낮을 수 있다). 원래 `NUM_RX`(V0)의 18/35·0/32는 가드만 본 기록이고 V0는 T에서 16/119가 오탐이다. "규칙 경보 ⇒ p≈1"은 측정한 곳(가드)에서만 성립하므로 S3.1이 규칙 정밀도 P(U 대 G∪T)로 정한다: P ≥ 1/(1+s)면 `false` + 규칙 구간, 0.5 ≤ P < 1/(1+s)면 `false/[]`(`output.rule_emit_spans: false`), P < 0.5면 규칙을 끈다. 구간(`rule_span: pm4`, `q6_rule_fp.py`의 `merged_pm4_spans`와 같은 방식): 걸린 토큰 중 서로 다른 문자열마다 그 문자열을 가진 첫 토큰의 위치(원본은 `ans.find(t)`라 "13개 총 3개"의 `3개`를 `13개` 안에서 찾는다. 여기서는 실제 토큰 위치, 선형) 하나에 ±4자 창을 만들고(같은 토큰이 여러 번 나와도 첫 위치만 쓴다), 겹치는 창은 하나로 합치고, 같은 구간은 한 번만 낸다. 여기에 `scoring.normalize` 뒤 1글자 이상 남는 구간만 낸다는 필터를 더한다(`merged_pm4_spans`에는 없는 추가 조건). (이 방식의 구간 F1: V2 경보 행에서 BM25 1위 기준 0.651, 정답 섹션 기준 0.638. 합치지 않은 V0의 0.627은 기록).
- `literal_rule`은 점수 차를 인자로 받는다: `literal_rule(answer_raw, hits, margin, kb, cfg)`. `hits`·`margin`은 같은 `Ranking`(`KbIndex.rank(query)` 한 번)의 `hits`·`margin`이고 `margin < cfg.output.rule_min_margin`(기본 3.5)이면 `None`이다.
- `rules.py`는 런타임에 `competition/grader`를 import하지 않는다. `scoring.normalize` 4줄 함수와 그것이 쓰는 `PUNCT` 상수를 함께 복사해 두고, 단위 테스트가 `scoring.normalize`와 출력이 같은지 대조한다(provenance 기록).
- 응답 직렬화는 `GuardResponse.to_json(block)`(block = 설정 `block_categories`) 하나다. `has_candidate`면 `output`을 반드시 넣는다(GR-01).
- 뷰(GR-02): 정규화본 = `normalize()`(NFKC 안 씀, LLM·kNN·LR용). 신호별 뷰는 측정한 뷰 그대로다. 거절 정규식 = 후보 답변 원문에서 인용(“” "" ‘’ '' 「」 『』)·백틱·코드 블록을 뺀 것(튜터 거절 36/36, 순응 답변 오감지 0/19, R13-A §6.4. S2.3에서 다듬음(council D14): 거절에 단독으로 걸리지 않던 4갈래 삭제, 토큰 사이 공백 `\s*`. 다듬은 뒤에도 36/36·0/19, 거절이 아닌 튜터 답변 오탐 11 → 4, 두 칸·NBSP·줄바꿈·붙여쓰기 변형 36/36) / 숫자·리터럴 = 원문 / 저신호 = 조합 전 텍스트(`strip_tags(원문)`: 태그 문자만 지운 원문). 붙인 뷰 = `joined(정규화본)`(한 음절짜리 한글 토큰이 공백 하나를 사이에 두고 2개 이상 이어진 구간의 공백만 지움, "광 고" → "광고")는 **구현하지 않는다**(필요하면 ablation으로 추가). 어떤 기본 신호도 읽지 않고 토글도 없다(다듬기 전 거절 정규식에 걸면 "알려 줄 수 없어요"가 붙어 거절 33/36이었다. `\s*`로 다듬은 뒤로는 붙여쓰기도 잡는다). NFKC는 `rules.py`의 숫자·리터럴 비교(양쪽 `scoring.normalize`)와 BM25 토큰에서만 쓴다.

### 4.2 판정 흐름 (`guard.py`)

```python
class Guard:
    @classmethod
    async def build(cls, cfg: GuardConfig, *, llm: LLM | None = None,
                    rows: Sequence[LabeledRow] | None = None) -> "Guard"
        # 기동 때 KB 색인·kNN 색인·LR 학습(합계 ≤30초). rows=None이면 공개 400행.
        # llm=None이면 build가 LLM을 만들고 LLM.start()(probe 1회)를 부른다(서버 lifespan 경로).
        # 주입한 llm은 이미 start()된 것으로 보고 probe를 다시 부르지 않는다. probe는 프로세스당 1회다.
        # 하네스는 LLM 하나를 만들어 start()한 뒤 모든 폴드의 build에 주입하고, 폴드마다 평가 묶음을 뺀 rows를 넘긴다(ADR-0013).
        # 만든 뒤 색인은 바뀌지 않는다
    async def judge(self, req: GuardRequest) -> Judgement     # 예외를 밖으로 내지 않는다(GR-09). 서버는 .response만 직렬화(S1.2 ConstantGuard와 같은 계약)
    async def classify(self, messages: Sequence[Message]) -> Category   # judge의 입력 부분. /tutor 제안(PRD §9 T1)
    async def judge_output(self, messages: Sequence[Message], candidate_answer: str,
                           category: Category | None = None) -> OutputVerdict
        # None이면 입력·출력 nano를 병렬로 부른다(최대 2회). 3단 ①과 safe는 그 범주를 쓴다. /tutor 제안(PRD §9 T1-2)
```

`judge()` 순서(요청 마감 = 시작 + `budget_s`):
1. ① 뷰를 만든다. 대상 = 마지막 `user` 메시지. 창 = 첫 학습자 발화 + 마지막 메시지 `input.window_last`개(첫 학습자 발화가 이미 마지막 `window_last`개 안에 있으면 중복해서 넣지 않는다).
2. ② 신호. `low_signal_direct`가 켜졌고 저신호면 범주는 `low_signal_input`, 입력 nano를 부르지 않는다. 저신호 검사는 **자모 조합 전** 텍스트(`preprocess.strip_tags`, 태그 문자만 지운 것)에 건다. "ㅋㅋㅠㅠ"가 음절로 조합되지 않게 하기 위해서다. 숨긴 태그(`has_hidden_tags`)는 길이 상한으로 자르기 전 후보 답변 원문 **전체**에 건다.
3. ③ kNN 예시·다수 범주, LR 예측. ④ `KbIndex.rank(query)`를 한 번 불러 상위 `kb.top_k`·점수 차·1위 섹션을 그 `Ranking`에서 읽는다(질의 = 창 안의 학습자 발화 + 후보 답변. 측정한 질의는 모든 user 발화 + 답변(`all_user + "\n" + candidate_answer`)이다. 가드 공개 세트는 메시지가 5개 이하라 창(첫 학습자 발화 + 마지막 6개) 안의 학습자 발화와 같다. 튜터 공개 세트는 최대 11메시지(7개 초과 58행)라 창과 다르지만, 창 질의로 q6를 다시 돌려도(`q6_rule_fp.py --window`) U·G·T 수치와 F1이 같았다([R13-A §6](research/R13-public-set-analysis.md#6-규칙-②-오탐-재측정-santa-4라운드))). 길이 상한: 메시지 하나는 보이지 않는 문자만 먼저 지우고(정규식, 선형) `input.max_chars`로 자르고(넘으면 공백·반복을 줄인 뒤 앞 + 뒤), 후보 답변은 전처리 전에 `input.max_answer_chars`로 자른다(1MB 메시지를 전처리하면 CPU 약 1.3초라 자모 조합 같은 무거운 정규화는 자른 뒤에 한다. 답변 상한은 SPEC §4의 답변 최대 4000자보다 크다). 출력 nano·규칙 ②·거절 정규식은 자른 답변을 보고, 숨긴 태그 검사만 자르기 전 전체를 본다(위 2). BM25 질의 = 창 안의 학습자 발화(`retrieval.max_query_chars // 2`자로 자름) + 후보 답변(`retrieval.max_query_chars // 2`자로 자름), kNN·LR 입력 = 판정 대상 발화(`retrieval.max_query_chars`자로 자름). 기본 4000자이고 공개 세트 학습자 발화는 최대 444자라 측정값에 영향 없다(학습자 부분은 뒤쪽 = 최근 발화를 남김). CPU가 큰 단계(전처리·BM25·kNN·LR)는 스레드 풀에서 돌린다(S2.10이 이벤트 루프를 확인).
4. ⑤·⑥을 태스크로 띄우고 마감까지 기다린다. ⑥은 후보 답변이 비어 있지 않으면 부른다(`nano_tier3: false`이고 `output.call_nano_when_unused: false`면 건너뜀. 기본 true라 측정 때는 늘 부른다). `output.mode: v6`이면 `output_judge.md`로 부르고 그 값은 ③(토글 `nano_tier3`가 켜졌을 때만)과 S3.1 정밀도 기록에 쓴다. `output.mode: baseline`이면 `output_judge_baseline.md`(reason → grounded → safe)로 부르고 그 값을 grounded·safe에 바로 쓴다(`unsupported=[]`).
5. ⑦–⑨ `decide.py`. 마감을 넘기면 끝난 결과 + GR-09 기본값으로 바로 응답하고 남은 태스크는 취소한다.

- 출력 nano의 문맥(R14-D4): 1위 섹션 전체(`section_text`, `output.context_max_chars`로 자름) + 상위 `kb.top_k` 조각 중 **다른 섹션**의 조각. 같은 `Ranking`에서 읽는다. 규칙 ②가 보는 범위(1위 섹션 전체)는 그대로다.
- 후보 답변이 `""`이면 출력은 `true/[]`, `safe=true`다(응한 내용이 없다, §8 미결 2). 출력 nano를 부르지 않는다.
- `candidate_answer`가 문자열이 아니면(`has_candidate=True`, `candidate_answer=None`) 출력에 한해 요청 파싱 실패로 본다: 출력 nano 실패와 같이 GR-07 표를 nano 없이 탄다(block 범주면 ① `true/[]`, 그 밖은 `output.failure_default`), `safe`는 GR-08 그대로(읽을 답변이 없어 거절·숨긴 태그 없음). 입력은 정상 판정한다.
- 요청 파싱 실패, 빈 `messages`, 학습자 발화가 하나도 없는 요청: 범주 `normal`, 오류 코드 `no_user_message`. `has_candidate`면 `output`은 `output.failure_default`(기본 `true/[]`), `safe=true`. 단 답변이 문자열이고 숨긴 태그가 있으면 `safe=false`다(GR-08은 발화가 없어도 본다)(GR-09. 출력 nano 실패와 같은 규칙 하나).

### 4.3 LLM 호출 (`llm.py`)

```python
Role = Literal["input_judge", "output_judge"]
ErrorCode = Literal["timeout", "rate_limit", "server", "bad_json", "auth", "bad_request", "budget"]

@dataclass(frozen=True)
class LLMResult:
    parsed: BaseModel | None      # 스키마 검증을 통과한 것만
    error: ErrorCode | None
    attempts: int                 # 1 또는 2
    latency_ms: float
    finish_reason: str | None     # 마지막 시도의 finish_reason("stop"·"length" 등). 응답이 없으면 None
    completion_tokens: int | None # 마지막 시도의 usage.completion_tokens. 없으면 None

class LLM:
    def __init__(self, cfg: LlmCfg, *, send: Send | None = None, clock=time.monotonic, sleep=asyncio.sleep)
    async def start(self) -> None                                        # 프로세스당 1회: 클라이언트를 만들고 probe()를 부른다. 두 번째 호출은 아무것도 하지 않는다
    async def probe(self) -> Mapping[str, ErrorCode | None]              # start()가 부른다. 파라미터마다 따로 시험, 거절된 것만 끈다(self.params). 결과는 오류 코드로만 기록
    async def complete(self, role: Role, messages: list[dict], schema: type[BaseModel], *,
                       deadline: float) -> LLMResult                     # deadline = 요청 마감(monotonic)
    def slot(self) -> AsyncContextManager[None]                          # 튜터가 같은 게이트를 쓰는 입구(PRD §9 T4)
    calls: Counter[Role];  max_calls: int | None                         # 하네스 예산(넘으면 error="budget", 호출 안 함)
```

- 클라이언트: `start()`에서 `AsyncOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, timeout=Timeout(connect=3, read=12, write=5, pool=5), max_retries=0)` 하나(R13-B2 §3-6).
- 요청: `model=OPENAI_MODEL`, `reasoning_effort="low"`(minimal 금지), `verbosity="low"`, `max_completion_tokens` ≥800, `response_format` json_schema strict. `temperature`·`max_tokens`·`logprobs`는 보내지 않는다(GR-10).
- `probe()`: `reasoning_effort`·`verbosity`·`max_completion_tokens`·`response_format` json_schema를 **하나씩 따로** 시험하고 거절된 것만 끈다(json_schema가 거절되면 json_object로 내린다). 설정에서 `null`인 파라미터는 보내지 않는다.
- 메시지 배치: system = 프롬프트 파일(고정), user = 요청별 부분(예시·대화·답변·KB). 접두가 같아 프롬프트 캐시가 맞는다(GR-10).
- 재시도: 호출마다 1회. 대상은 연결 오류·타임아웃·408·409·429·5xx·스키마 불일치. 대기는 `retry-after-ms` → `retry-after` → 지터 0.2–0.5초. **대기 + 읽기 한도가 마감을 넘으면 재시도하지 않는다.** 401·403·400·422는 바로 실패.
- 게이트: `asyncio.Semaphore(llm.gate)` 하나(기본 64). 게이트 대기도 마감 안에 든다. 허가는 `finally`에서 돌려준다.
- 예외를 밖으로 내지 않는다(`CancelledError`만 나간다). 로그·`error`에는 오류 코드와 예외 클래스 이름만 남긴다. 예외 메시지·`body`는 본문을 담을 수 있다(GR-12, S6).
- 출력 스키마(pydantic, `extra="forbid"`). S1.3이 `judges/schemas.py`에 만들고 테스트한다. S2.6·S2.7은 이 스키마에 맞춰 프롬프트만 쓴다:
  - `InputOut{evidence: str (≤1문장), category: Category}` — 이 순서(GR-04)
  - `OutputOut{answer_quote: str, kb_quote: str, grounded: bool, refuses: bool}` — 이 순서(GR-07, R14-D2·D3. S1.3이 만든 `{reason, grounded}`를 S2.7이 바꾼다). `answer_quote` = 답변에서 틀렸거나 근거 없는 구절 그대로(80자 이하, 근거 있음이면 빈 문자열), `kb_quote` = 그 구절과 **어긋나는** 자료 구절 그대로(80자 이하, 자료에 관련 내용이 없으면 빈 문자열), `refuses` = 답변이 요청을 거절하는가(기록만). 베이스라인 전용 `OutputBaselineOut{reason, grounded, safe}`(§4.4 `output.mode: baseline`)
- 테스트는 `tests/fakes.py`의 `FakeLLM`(호출마다 `Ok`·`Malformed`·`Timeout`·`RateLimited(retry_after)`·`ServerError`·`BadRequest`를 스크립트로)과 주입한 시계로 0ms에 돈다. 실제 API는 `-m live`에서만.

### 4.4 설정 (`configs/*.yaml`)

```yaml
version: "6.0.3"                   # 설정·프롬프트·분할을 바꾸면 올린다. config_hash에 `prompts` 각 파일의 머리말 version도 든다
paths: {kb: ../competition/kb, public: ../competition/public, prompts: prompts}   # 환경변수로 덮어쓸 수 있다(아래)
prompts: {input_judge: input_judge_en.md, output_judge: output_judge_en.md, output_judge_baseline: output_judge_baseline.md}   # R14-D9. 이름 → paths.prompts 아래 파일(id·version 머리말 필수). config_hash가 각 파일의 version을 읽는다. S2.6·S2.7이 input_judge·output_judge·output_judge_baseline을 더한다
budget_s: 20                       # 요청 예산(GR-09). 규격은 30초
block_categories: [jailbreak_injection, harmful_content, academic_dishonesty, off_topic]   # 범주 → decision 표(ADR-0012)
llm:                               # 파라미터는 모두 null 가능(null = 보내지 않음). probe()가 거절된 것만 끈다
  reasoning_effort: low
  verbosity: low
  max_completion_tokens: 1600      # 800이면 출력 판정 호출의 6–9%가 잘렸다(finish_reason=length). 1600에서 잘림 0/192(S2.9, results/2026-11-13-v1.md). 대신 read 12초 시간 초과가 유효 6회에서 8/384(2.1%)다(실질 상한 약 1,390토큰, 조치는 ADR-0014 S2.9 수정). 입력 판정은 800에서도 잘림이 없었다
  structured_output: json_schema   # json_schema | json_object. probe()가 내린다
  timeout_s: {connect: 3, read: 12, write: 5, pool: 5}
  retries: 1
  retry_jitter_s: [0.2, 0.5]
  gate: 64
toggles:                           # GR-11. 모두 false + output.mode: baseline = "nano 단독" 베이스라인(configs/baseline.yaml)
  knn_fewshot: true                # GR-05 few-shot 예시만 끈다(kNN 다수결은 knn_vote)
  knn_vote: true                   # GR-05 kNN 다수결(실패 기본값·3번째 호출 투표). PRD GR-09 "kNN이 꺼져 있으면"은 이 키
  lr: true                         # GR-05 LR(기본 범주·3번째 호출 투표)
  third_call: false                # GR-05 ③, P2
  low_signal_direct: true          # GR-06 저신호 직접 판정
  literal_rule: true               # GR-07 ②
  nano_tier3: true                 # GR-07 ③ 사용 여부(출력 nano 호출 자체는 v6 모드에서 `output.call_nano_when_unused: true`인 동안 늘 함). S3.1이 재기 전의 잠정 기본값. S3.1이 output.failure_default와 함께 4조합을 비교해 정한다
  nano_span: true                  # GR-07 ③에서 확인된 answer_quote를 구간으로 낸다(ADR-0014 S2.9 수정). 자른 원문에 글자 그대로 있는 인용만 낸다. 끄면 ③은 false/[]. ③의 "근거 없음" 정밀도가 0.8 아래로 떨어지면 끈다
  safe_rule: true                  # GR-08 safe 규칙만. GR-07 ①(block 범주 → true/[])은 output.mode: v6에서 토글 없이 늘 적용
  oom_kb_snippet: false            # GR-04 out_of_material용 KB 조각, P1
  embedding: false                 # GR-03·GR-05 임베딩, P2
input:
  window_last: 6
  max_chars: 2000                  # 메시지 하나의 원문 길이 상한(전처리 전에 자름. 판정 대상 <target>에도 적용, 긴 이력 대비)
  max_answer_chars: 8000           # 후보 답변의 원문 길이 상한(전처리 전에 자름. SPEC §4 답변 최대 4000자). 출력 nano·규칙 ②·거절 정규식이 자른 답변을 본다. has_hidden_tags는 자르기 전 전체
  knn: {analyzer: char_wb, ngram: [1, 3], max_examples: 8, per_category: 2}
  knn_vote_k: 7                    # kNN 다수결 이웃 수(실패 기본값·3번째 호출)
  lr: {analyzer: char_wb, ngram: [1, 3], C: 100}
  oom_snippet_chars: 400
kb: {top_k: 3, ngram: 2, k1: 1.2, b: 0.75}
retrieval: {max_query_chars: 4000}   # BM25 질의(학습자 부분·답변 부분 각 //2)·kNN·LR 입력(발화 하나) 상한(긴 이력 대비, 뒤쪽을 남김)
output:
  mode: v6                         # v6 | baseline(출력 nano 베이스라인 프롬프트 → grounded·safe 바로, unsupported=[])
  failure_default: grounded        # grounded(true/[]) | ungrounded(false/[]). 토글 아님. S3.1이 정한다(PRD GR-07·GR-09)
                                   # 출력 nano가 실패했을 때와 nano_tier3가 꺼져 nano 판정을 쓰지 않을 때의 ④에만 적용한다.
                                   # nano_tier3가 켜져 있고 nano가 근거 있음이라고 한 ④는 늘 true/[]다
  call_nano_when_unused: true      # nano_tier3가 꺼졌을 때 출력 nano를 그래도 부를지. 측정 때는 true(S3.1 정밀도 기록). nano_tier3: false로 동결하면 false로 낮춰 호출을 아낄 수 있다
  rule_min_margin: 3.5             # Ranking.margin(KbIndex.rank 한 번, 전체 순위, 섹션별 최고 점수) 기준. S3.1이 G∪T(가드 근거 있음 + 튜터 참조 답변) 오탐으로 정한다. 기본 3.5는 공개 세트에서 정한 잠정값(R13-A §6.2)
  rule_emit_spans: true            # true = 규칙 ② → false + 규칙 구간 | false = false/[]. S3.1이 규칙 정밀도 P로 정한다(P ≥ 1/(1+s) → true, 0.5 ≤ P < 1/(1+s) → false, P < 0.5 → literal_rule 끔)
  rule_span: pm4                   # pm4(서로 다른 토큰 문자열마다 첫 토큰 ±4자, 겹치는 창 병합·중복 제거, `merged_pm4_spans`, F1 0.651 측정값) | eojeol(어절 경계). S3.1이 비교
  rule_extra_tokens: []            # ablation: value_norm(콤마·만·천·억) | units_extra(주·일·년·글자) | bare_numeral(단위 없는 한글 수사, 미구현: 설정하면 기동 실패) | backtick_latin(백틱 안 영문만). 기본 [] = V2(NUM_RX + 인용 제거 + 수사 클래스 수정)
  rule_require_same_kind: false    # ablation(미구현: true면 기동 실패): 같은 종류(단위 숫자/한글 수사/백틱 코드/함수 호출/리스트 리터럴)의 다른 값이 섹션에 있을 때만 걸림
  tier3_trust: answer_quote        # all | answer_quote | both_quotes. nano "근거 없음" 가운데 ③에 쓰는 것(R14-D2). 조건에 못 미친 "근거 없음"은 ④의 output.failure_default. 잠정값이고 S3.1이 저장된 응답으로 정한다. S2.7·S2.8이 더한다
  quote_min_chars: 8               # answer_quote 확인에서 정규화 뒤 최소 글자 수(잠정. "3개월" 같은 짧은 인용이 떨어진다. S3.1이 0과 비교)
  context_max_chars: 4000          # 출력 nano에 주는 1위 섹션 전체의 길이 상한(R14-D4. 공개 KB 섹션은 CSV 포함 최대 약 2,700자라 기본값에서는 잘리지 않는다)
embedding: {targets: [kb, knn], weight: 0.5}   # embedding 토글이 켜졌을 때만
eval: {max_calls: 5000}            # 하네스 실행당 API 호출 상한(--max-calls로 덮어씀)
```

- `extra="forbid"`로 오타를 로드에서 잡는다. 검증: `third_call`은 `knn_vote`·`lr`이 둘 다 켜졌을 때만, `nano_span`에는 조합 검증이 없다(S2.9에서 기본이 켬이 되면서 뺐다. `nano_tier3`가 꺼지면 구간을 낼 자리가 없을 뿐이다. S2.8의 `nano_tier3` 조건과 그 앞의 `literal_rule` 조건은 기록). `output.rule_extra_tokens`의 `bare_numeral`과 `output.rule_require_same_kind: true`는 미구현이라 로드 때 거부한다.
- 베이스라인(`baseline.yaml` = 토글 모두 false + `output.mode: baseline`): 입력은 `input_judge.md`에서 예시 블록만 비운 것(범주 정의·짝 대조 문구는 유지)을 쓴다(따로 파일을 두지 않는다). 실패하면 `normal`. 출력은 `output_judge_baseline.md`(reason → grounded → safe)의 nano 값을 grounded·safe에 바로 쓰고 `unsupported=[]`. decision은 그래도 범주에서 도출한다.
- `configs/ablation/*.yaml`은 `default.yaml` 위에 덮어쓸 키만 담는다(깊은 병합). 이름은 `no_<토글>.yaml`(기본 켬을 끔), `with_<토글>.yaml`(기본 끔을 켬).
- 키·주소는 설정 파일에 쓰지 않는다. 환경변수: `TEAM_TOKEN`, `OPENAI_BASE_URL`·`OPENAI_MODEL`·`OPENAI_API_KEY`, `EMBEDDING_BASE_URL`·`EMBEDDING_MODEL`·`EMBEDDING_API_KEY`(임베딩 ablation만), `GUARD_CONFIG`(설정 경로, 기본 `configs/default.yaml`), `GUARD_KB_DIR`·`GUARD_PUBLIC_DIR`·`GUARD_PROMPTS_DIR`(있으면 `paths.kb`·`paths.public`·`paths.prompts`를 검증 전에 덮어씀, 런박스 경로용). `GUARD_*`는 S1.1의 `load_config()`가 읽고 테스트한다. `${ENV}` 치환은 없다. 런박스는 `/home/elicer/competition/.env`를 셸에서 읽는다(`uvicorn --env-file`은 python-dotenv가 필요해 쓰지 않는다).

### 4.5 데이터 (`data.py`, `eval/cv.py`)

```python
@dataclass(frozen=True)
class PublicRow:
    id: str;  endpoint: Literal["guard", "tutor"]
    messages: tuple[Message, ...];  candidate_answer: str | None
    labels: Mapping[str, Any]          # 원본 그대로(category·decision·grounded·unsupported·safe·action·evidence·reference_answer)

def load_public(public_dir: Path) -> list[PublicRow]            # guard.jsonl + tutor.jsonl, 400행
def labeled_rows(rows: Iterable[PublicRow]) -> list[LabeledRow]
```

- KB: `kb/<과목>/<섹션 ID>.md` 48섹션을 색인하고 같은 이름의 `<섹션 ID>-table<n>.csv`(23개)를 그 섹션에 붙인다. HTML(1개)은 같은 단원의 중복이라 읽지 않는다. `##` 소제목 단위 조각(`common.kb_chunks` 그대로: `## ` 줄 앞에서 자르고 코드 블록 안도 자름, §3 기존 코드 처리 표, GR-03).
- **짝 묶음**(`eval/cv.py`): 400행의 **마지막 학습자 발화**(정규화본) 문자 3-gram Jaccard ≥0.5로 이은 연결 요소(GR-13). 빠른 질문 버튼 첫 발화는 여러 행이 같은 문장을 써 무관한 행을 이으므로 넣지 않는다. 가장 큰 묶음 크기를 S1.4에서 보고한다.
- **분할 파일** `src/guardrail/eval/splits/v1.json`(커밋): `{version, jaccard, folds: 5, seed, cluster: {id: cid}, fold: {cid: k}, holdout: [cid]}`. 홀드아웃 = 가드 행 기준 20%의 묶음. 나머지 묶음을 가드 행 수가 고르게 5분할한다. 만든 뒤 바꾸지 않는다(바꾸면 `v2`).
- 폴드 k를 평가할 때 kNN·LR은 폴드 k와 홀드아웃 묶음을 뺀 행으로 만든다. 홀드아웃을 평가할 때는 홀드아웃 묶음만 뺀다(ADR-0013).
- 홀드아웃 열람은 `splits/holdout_views.json`에 날짜·커밋·`config_hash`로 남기고, 4번째 실행은 하네스가 거부한다(GR-13, 최대 3회).

### 4.6 평가 CLI (`eval/`)

```bash
uv run python -m guardrail.eval.cv --write                      # 묶음·폴드·홀드아웃 파일(한 번)
uv run python -m guardrail.eval.run --config configs/default.yaml --name v1 \
    [--set toggles.lr=false ...] [--repeat 3] [--max-calls 5000] [--estimate] [--fake] [--holdout] [--input-only]
uv run python -m guardrail.eval.run --ablation configs/ablation/ --name abl-1117 --repeat 3
uv run python -m guardrail.eval.run --ablation configs/ablation/ --only no_literal_rule,no_nano_tier3 --name abl-1118a --repeat 3   # --only: 이름으로 부분 실행. 기준 행(baseline·default)은 --only와 무관하게 늘 포함
uv run python -m guardrail.eval.load --url http://localhost:8000 --token "$TEAM_TOKEN" --concurrency 64 --n 600
uv run python -m guardrail.eval.probe --calls 200               # 런박스 실측(PRD §11-2-1)
```

- `run`은 서버를 띄우지 않는다. LLM 하나를 만들어 `start()`(probe 1회)한 뒤 폴드마다 `await Guard.build(cfg, llm=llm, rows=…)`로 `judge()`를 직접 부른다. 응답은 `eval/score.py`가 `competition/grader/scoring.py`의 `score_guard`·`guard_score`와 `harness/score_local.py`의 `as_scoring_input`을 그대로 import해 채점한다. `score_guard`·`guard_score`에는 `endpoint=='guard'` 행만 넣는다. 교차 검증 가드 점수는 홀드아웃을 뺀 가드 행(약 160행)으로 보고하고, 홀드아웃 가드 행(약 40행, 20%)은 `--holdout`으로 따로 잰다. 튜터 행(decision 라벨 없음)은 범주 정확도와, 범주에서 도출한 decision으로 block F1을 따로 계산한다(`eval/score.py`가 합성 dict를 `scoring.block_f1`에 넘겨 계산한다).
- 실행 전에 예상 호출 수를 찍고(probe 호출은 폴드 수와 무관하게 실행당 한 번으로 센다) `--max-calls`(기본은 설정 `eval.max_calls` = 5,000)를 넘으면 멈춘다. `--estimate`는 호출 0회, `--fake`는 `FakeLLM` 고정 응답(입력 `normal`, 출력 grounded)으로 API 0회.
- 산출: `runs/<name>/responses.jsonl`(공개 세트 응답 + `Judgement` 요약, 배포 자료 분석이라 서버 밖에서 저장 가능), `summary.json`, `report.md`. `runs/`는 커밋하지 않는다. 남길 표는 `results/<날짜>-<이름>.md`로 옮겨 커밋한다.
- `report.md` 내용: 가드 점수(3회 범위), block F1·정밀도·재현율, 짝 문항만의 block 정확도, 범주 정확도(non-normal)와 혼동행렬, 출력 평균·항목별·단별(①–④) 적중, kNN·LR 단독 점수, 튜터 200행 범주 정확도(따로), 호출 수와 요청당 호출 수, 역할별(입력·출력) 오류 코드 비율(`bad_json`·`timeout` 등)과 `finish_reason=length` 비율(실제 프롬프트로 `max_completion_tokens`를 다시 확인, S1.6 값은 잠정), 지연 p50·p95. 설정 비교는 묶음 단위 부트스트랩 95% 구간을 보고한다(채택 기준은 아님, PRD §7).
- `--input-only`(R14-D1): `Guard`(S2.8) 없이 입력 판정만 잰다. 저신호 직접 판정 + kNN few-shot + `judge_input`으로 범주를 내고 출력은 상수다(행당 nano 0–1회, 실행당 약 320회). 입력 프롬프트 v1을 출력 판정·조립보다 먼저 재는 데 쓴다. S2.3·S2.5·S2.6이 머지되면 돌 수 있다. 첫 실측(1회) 뒤 같은 설정으로 2회를 더 돌려 v1의 3회 범위(잡음 바닥)를 만든다(2026-11-13 조정, [결과](../results/2026-11-13-input-v1-solo.md)).
- 모든 실측 실행은 역할별 실패율(`finish_reason`이 `stop`이 아님·파싱 실패·시간 초과)을 report 맨 위에 낸다. 어느 역할이든 2%를 넘으면 프롬프트 문구보다 길이·문맥부터 고친다(R14-D8). 출력 쪽은 잘림(`finish_reason=length`)이 2%를 넘을 때 `kb_quote`를 먼저 빼고, 그다음 `output.context_max_chars`를 낮춘다. 시간 초과는 이 규칙에 넣지 않고 역할별 실패율 2%로 따로 센다. 시간 초과의 조치는 문맥 축소가 아니라 `read` 한도·토큰 상한을 API가 돌아오면 재서 고르는 것이다(ADR-0014 S2.9 수정).
- 하네스가 실측을 무효로 남기는 경우(종료 코드가 0이 아니고 report 맨 위에 무효 표시): 호출 상한에 걸림(5), 429가 20회 이어짐(6), 입력 단독의 입력 판정 실패율이 2%를 넘음(8), 인증 오류 1회 또는 429·서버 오류·시간 초과가 섞여 20회 이어짐(9, 호출을 멈춘다), 어느 역할이든 호출 실패율이 실행 전체나 한 회차에서 10%를 넘음(8). 실패한 호출은 기본값으로 채워져 점수를 바꾸기 때문이다. 2%를 넘고 10% 이하면 무효가 아니고 report 맨 위에 경고만 낸다. 10%는 재지 않고 정한 잠정값이다. 유효 6회의 출력 시간 초과 8/384(2.1%)를 무효로 만들지 않으면서 절반이 실패한 실행은 걸러내는 값으로 골랐고, 시간 초과 조치를 잰 뒤 다시 정한다(#151 리뷰).
- `load`는 공개 세트의 가드·튜터 요청을 동시 64로 다시 보내 시간 초과·형식 오류·3xx·p50·p95를 낸다. 채점 수준(약 600요청)을 넘는 시험은 하지 않는다(GR-10).

### 4.7 의존성

| 용도 | 고른 것(R13-B2 §4) | 넣지 않는 것 |
|---|---|---|
| 서버 | `fastapi==0.162.2`, `uvicorn==0.54.0`(`[standard]` 아님, `--no-access-log`) | `uvicorn[standard]`, uvloop |
| LLM | 기존 `openai>=3,<4`(httpx2 내장) | 런타임 `httpx`, LiteLLM, tiktoken(실행 중 파일 다운로드) |
| kNN·LR | `scikit-learn==1.9.1`(scipy가 런타임으로 올라감). 기동 때 학습, pickle 없음 | bm25s, rank_bm25, fasttext, 임베딩·분류 모델 파일 |
| 구간 맞추기(P2) | `rapidfuzz==3.14.6` | difflib |
| 형태소 | 쓰지 않음(문자 n-gram) | kiwipiepy·mecab(R13 D7) |
| 검증·테스트 | `pydantic`, `pyyaml` / dev `jsonschema==4.26.0`, `pytest`, `pytest-cov`, `hypothesis`, `anyio`, (TestClient가 요구하면) `httpx` | |

새 런타임 의존성을 넣는 PR은 `competition/provenance.md` 표에 행을 더한다(GR-14).

## 5. 슬라이스 목록

예상 시간은 집중 작업 기준이다. 모델: **Opus** = 판정·프롬프트·규칙 설계, santa 리뷰, 측정 해석 / **Sonnet** = 기계적 구현·실행(서브에이전트, worktree).

| ID | 슬라이스 | GR / ADR | 우선 | 예상 | 모델 | 선행 | 수용 기준(요지) |
|---|---|---|---|---|---|---|---|
| **S1.1** | v4 코드 정리·의존성·설정 v6 | 11·14 | P0 | 3h | Sonnet | — | §3 삭제 목록 반영(`mock_tutor.py` 포함), v4 `llm.py`·`test_llm.py`·`fakes.py`·`unguarded.yaml`도 삭제(S1.3이 LLM 계층을 새로 씀), `config.py`·`test_config.py`를 §4.4 모델로 다시 씀, `default.yaml`·`baseline.yaml` 로드, 토글 검증·`config_hash`(프롬프트 머리말 version)·`GUARD_CONFIG`·`GUARD_*_DIR` 덮어쓰기 테스트, `Signals`·`Judgement` dataclass, 남은 코드로 CI green(커버리지 ≥80%) |
| **S1.2** | 서버 골격·스키마(인증·200 보장·로그 규칙, 상수 판정) | 01·09·12 / 0009 | P0 | 5h | Sonnet + 보안 리뷰 | S1.1 | `ConstantGuard.judge()` → `Judgement`(서버는 `judgement.response`만 직렬화), `TEAM_TOKEN`은 lifespan에서 읽음(없으면 기동 실패), 퍼징·필드 누락에서 형식 오류 0·422 0·3xx 0·본문 반사 0(오류·기본값 경로와 `input` 객체. `unsupported`는 후보 답변의 원문 구간이라 예외), 인증은 가드 `APIRouter`의 의존성(`APIRouter(dependencies=[Depends(check_token)])`)이고 `create_app()`이 복사한 `/tutor`에도 같은 검사를 건다, 인증 실패 401(`/guard`와 `create_app()`의 `/tutor` 둘 다, 라우터만 맨 FastAPI 앱에 붙여도 토큰 없으면 401인 테스트), `setup_guard` 없이 라우터만 붙인 앱은 기동 실패, `create_app(guard_factory)`가 `GUARD_CONFIG`로 설정을 읽음, 핸들러 전체 try/except와 `uvicorn.error` 로그에 예외 메시지 0, 스키마 대조, 로그 grep 0건(GR-01·12) |
| **S1.3** | LLM 게이트 개편 | 10 / 0011 | P0 | 4h | Sonnet | S1.1 | `llm.py`·`tests/test_llm.py`·`tests/fakes.py`(`FakeLLM`·`FakeClock`) 새로 씀(v4 파일은 이력 참고), §4.3 규칙 테스트(재시도 1·마감·Retry-After·게이트·파라미터·`start()` 1회 probe·`finish_reason`·`completion_tokens`), 출력 스키마 `InputOut`·`OutputOut`·`OutputBaselineOut`(`judges/schemas.py`), 예외 메시지 미기록 |
| **S1.4** | 공개 세트 로더·채점 재현·짝 묶음 분할 | 13 / 0006·0013 | P0 | 4h | Sonnet(분할은 Opus 검토) | S1.1 | in-process 상수 응답을 `score.py`로 채점한 값이 R13-A Q4와 같음(전부 allow 29.48, 입력 완벽 + 상수 출력 84.48), `splits/v1.json` 커밋(최대 묶음 크기 보고). `score_local.py` 대조는 S1.5 |
| **S1.5** | 런박스 배포·상수 응답 첫 제출 | 01·14 / 0009 | P0 | 2h | 본인 | S1.2(채점 대조는 S1.4) | 터널 주소 200 JSON·리다이렉트 없음, 토큰 없는 `/guard`·`/tutor` 요청 401, 상수 서버를 `score_local.py --tutor-url --guard-url`(공개 400행, 시작용 튜터 nano 약 200회)로 채점해 `score.py`와 가드 점수 차 ≤0.005, `submission.json`에 커밋 해시, 채점 메시지 기록 |
| **S1.6** | 런박스 실측(nano 지연·파라미터) | 10 / 0011 | P0 | 2h | 본인(코드 초안은 Sonnet) | S1.3·S1.5 | PRD §11-2-1 표: 파라미터별 수용, json_schema 여부, `max_completion_tokens` 800 대 1200–1600의 `finish_reason=length` 비율(실제 길이의 대역 프롬프트: system 문구 + 예시 8줄 + 메시지 6개. 잠정값으로 기록, S2.9가 실제 프롬프트로 다시 확인), 동시 1·16·64 지연 p50·p95, 호출 ≤200 |
| **S2.1** | 전처리 v6(숨긴 문자 드러내기) | 02 / 0001 | P0 | 3h | Sonnet | S1.1 | 임의 유니코드 예외 0, `strip_tags`(태그 문자만 지움, 조합 없음), p95 ≤10ms, 기존 정규화 회귀 유지(GR-02. KB 코드 블록 오매칭 0은 S2.3) |
| **S2.2** | KB 색인·검색 | 03 / 0010 | P0 | 4h | Sonnet | S1.4·S2.1 | 튜터 `evidence` recall@1·@3 보고, block이 아닌 가드 후보 67행 정답 섹션 1위 ≥64/67·3위 ≥66/67, 색인 시간·메모리, `KbIndex.rank` 한 번으로 상위 k·점수 차·1위 섹션(역색인이면 `common.py` 점수와 1e-9 안), 검색 p95 ≤20ms(개발 머신, 가드 86 + 튜터 200 질의. 런박스는 S2.10에서 다시 재고(S1.6 시점에는 `kb.py`가 없음) 더 느리면 ≤40ms) |
| **S2.3** | 결정적 신호(저신호·거절·숨긴 태그) | 06·08 / 0014 | P0 | 4h | Opus | S1.4·S2.1 | 저신호 400행 정밀도 100%(표본 안 값: 규칙이 이 400행의 관찰에서 나왔다. 비공개 일반화는 가정), 거절 정규식 튜터 거절 36/36(표본 안 값)·순응 오탐 보고, LLM 없이 같은 값(GR-06), KB 코드 블록 신호 정규식 오매칭 0(GR-02) |
| **S2.4** | 숫자·리터럴 규칙 ② | 06·07 / 0014·0010 | P0 | 5h | Opus | S2.2 | 정확한 이식 검증: `rule_min_margin` 0에서 U 18/35·G 0/32·T 6/119, 3.5에서 16/35·0/32·3/119, 구간은 `q6_rule_fp.py`의 `merged_pm4_spans` 출력과 같은 스팬이고 구간 F1은 ±0.005 안(BM25 1위 0.651 / 정답 섹션 0.638, R13-A §6). 기본 `rule_min_margin`은 3.5. U·G·T 탐지·오탐을 따로 보고(튜터 0건은 요구하지 않음), 구간 100% 원문 부분 문자열 |
| **S2.5** | kNN·LR 보조 판정기 | 05·14 / 0013 | P0 | 5h | Sonnet | S1.4·S2.1 | 교차 검증 kNN 다수·LR 각각의 block F1·범주 정확도, 기동 학습 ≤10초, 요청당 p95 ≤20ms, 색인 불변 테스트 |
| **S2.6** | 입력 판정 nano·프롬프트 v1 | 04 / 0012·0013 | P0 | 8h | Opus + santa | S1.3·S2.5 | `FakeLLM`으로 스키마·재시도·기본값 계약, 기본값을 쓰기 전 nano 응답 성공률 ≥98%(재시도 포함), 실패율은 오류 코드별 보고, 호출 p95 ≤5s |
| **S2.7** | 출력 근거 판정 nano·프롬프트 v1 | 07 / 0014 | P0 | 5h | Opus + santa | S1.3·S2.2 | 입력 형식(발화·드러낸 답변·1위 섹션 전체 + 다른 섹션 조각, R14-D4), 스키마 `answer_quote → kb_quote → grounded → refuses`(R14-D2·D3), 인용 확인 함수(정규화 뒤 부분 문자열, `quote_min_chars`), 실패 시 `None` |
| **S2.8** | 판정 조립: decision·3단·safe·기본값·예산 | 07·08·09·01 / 0012·0014·0009 | P0 | 5h | Opus | S1.2·S2.3·S2.4·S2.6·S2.7 | GR-07 표 ①–④·GR-08·GR-09 표 전 행 테스트, 장애 주입에서 형식 오류 0·30초 안, 서버 연결, 입력 판정 KB 발췌에서 score==0 조각 제외(테스트), 규칙 ②·발췌가 hits[0]을 쓰기 전 hits[0].score > 0 확인, KB 로드 ValueError가 /tutor 기동까지 막으므로 제출 전 자가 점검이 잡는지 확인 |
| **S2.9** | 평가 하네스 v1·베이스라인 | 13·11 / 0006·0013 | P0 | 6h | Sonnet | S1.4·S2.8 | §4.6 `report.md` 전 항목, 3회 분산, nano 단독·kNN·LR 단독 점수(PRD §11-2-2), 설정만으로 ablation 실행. R14: `--input-only`로 입력 v1 단독 1회를 S2.8보다 먼저 잰다(D1), 역할별 실패율과 2% 관문(D8), nano 파싱 출력을 `runs/`에 저장, 저장된 두 실행을 호출 없이 비교하는 `--diff` |
| **S2.10** | 부하 시험·v1 제출 | 10·09 / 0011 | P0 | 3h | 본인(코드 초안은 Sonnet) | S1.6·S2.8 | 동시 64·약 600요청 시간 초과 0, 가드 p50·p95(목표 ≤8s), 전체 시간 약 2분 수준, v1 제출 기록 |
| **S2.11** | 튜터 공유 API(제안 T1·T2·T4) | 10 / — | P1 | 2h | Sonnet | S2.8 | `async Guard.classify`·`async Guard.judge_output(messages, candidate_answer, category=None)`(nano 재시도 0, 구현됨)·`KbIndex`·`LLM.slot` import 예제와 테스트, 리더에게 전달 |
| **S3.1** | 출력 측정·켜기 결정(③ 정밀도, 규칙 폭·점수 차) | 07 / 0014 | P0 | 4h | Opus | S2.9 | PRD §11-2-3·5 수치, `nano_tier3`·출력 실패 기본값(4조합 비교)·`tier3_trust`(저장된 응답으로, R14-D2)·`rule_span`·`rule_min_margin`(G∪T 기준)·규칙 정밀도 P → `rule_emit_spans`/규칙 끔 결정과 근거 |
| **S3.2** | 입력 오류 분석·프롬프트·예시 조정 | 04·05 / 0012·0013 | P0 | 8h | Opus + santa | S2.9 | 짝 묶음 교차 검증 block F1·짝 정확도 개선이 3회 분산 밖, 바뀐 프롬프트 santa 통과. 절차는 R14-D7: 변형마다 1회 선별 → 하나의 v2로 합쳐 3회 확정 1회. v2는 `gr/s3.2-input-v2`의 새 PR에서 `input_judge.md`의 `version`을 2로 올린다. S2.6 PR은 v1 그대로 머지한다(기준선 보존, §10) |
| **S3.3** | OOM용 KB 조각 ablation | 04 / 0010 | P1 | 3h | Sonnet | S2.9 | `with_oom_kb_snippet` 순이득(3회 분산 밖에서 > 0이면 켬, 부트스트랩 구간은 보고)과 켜기 결정 |
| **S3.4** | 3번째 호출 ablation | 05 / 0011·0013 | P2 | 4h | Sonnet → Opus 판정 | S2.9 | **동결 전 미실행(R14-D6).** v1 응답에서 "nano가 kNN·LR 둘 다와 엇갈린 행"의 nano 정답률만 호출 없이 계산해 남긴다. 0.5보다 낮을 때만 다시 올린다 |
| **S3.5** | nano 구간·임베딩 ablation | 07·03·05 / 0014·0010 | P2 | 5h | Sonnet | S3.1 | 각 순이득과 켜기 결정, 임베딩은 기동 시간·호출 수 함께 |
| **S3.6** | ablation 표·분산·지연 표 확정 | 11·13 / 0006 | P1 | 3h | Sonnet | S3.1–S3.5 | 베이스라인·전체·토글 하나씩, 3회 범위, 지연 표(GR-11·13 수용 기준). 모든 행(베이스라인 포함)을 S3.2 머지 뒤 동결 후보 프롬프트로 (다시) 잰다. 11/18 1차는 입력 프롬프트를 건드리지 않는 토글만이고, 프롬프트 버전이 다른 결과는 재사용하지 않는다 |
| **S3.7** | 홀드아웃·기능 동결(11/20) | 13 / 0006 | P0 | 3h | Opus | S3.1·S3.2(S3.3–S3.5는 11/19 12:00까지 결정된 것만 반영, 나머지는 꺼짐) | 동결 설정 1회 홀드아웃, 열람 기록, 동결 커밋 SHA, 동결본 제출 |
| **S4.1** | 규칙 준수 점검(provenance·로그·색인 불변·하드코딩) | 12·14 / 0009·0013 | P0 | 4h | Opus + 보안 리뷰 | S3.7 | provenance 서버 자산 전수, 로그 코드 리뷰 본문 경로 0, 공개 세트·퍼징 뒤 로그 grep 0건 |
| **S4.2** | 최종 부하·최종 제출 | 01·10·14 / 0009 | P0 | 3h | 본인 | S4.1 | 런박스 재시작 뒤 부하 시험 통과, `submission.json` 커밋 해시, 11/24 18:00까지 제출 |
| **S4.3** | 시연 자료(가드 부분) | GG2·GG4·GG5 | P0 | 4h | Sonnet 초안 → Opus | S3.6·S4.1 | 점수·ablation·지연 표, 로그 코드 설명, provenance, 리더에게 전달 |

Phase 합계: C1 20h · C2 50h(P1 2h) · C3 30h(P0 15h, P1 6h, P2 9h) · C4 11h. 합계 111h, P0 94h.

### GR 추적표 (모든 GR이 슬라이스에 걸린다)

| GR | 슬라이스 | GR | 슬라이스 |
|---|---|---|---|
| GR-01 HTTP 규격·인증 | S1.2, S1.5, S2.8, S4.2 | GR-08 safe 규칙 | S2.3, S2.8 |
| GR-02 전처리 | S2.1, S2.3 | GR-09 장애·기본값 | S1.2, S2.8, S2.10 |
| GR-03 KB 색인·검색 | S2.2, S3.5 | GR-10 동시성·지연·게이트 | S1.3, S1.6, S2.10, S2.11, S4.2 |
| GR-04 입력 범주 판정 | S2.6, S3.2, S3.3 | GR-11 설정·ablation | S1.1, S2.9, S3.6 |
| GR-05 kNN·LR | S2.5, S2.6, S3.2, S3.4, S3.5 | GR-12 로깅 | S1.2, S4.1 |
| GR-06 결정적 신호 | S2.3, S2.4 | GR-13 평가 하네스 | S1.4, S1.5, S2.9, S3.6, S3.7 |
| GR-07 출력 근거 판정 | S2.4, S2.7, S2.8, S3.1, S3.5 | GR-14 규칙 준수 | S1.1, S1.5, S2.5, S4.1, S4.2 |

PRD §11-2 측정 항목: 1 → S1.6, 2 → S2.9, 3·5 → S3.1, 4 → S3.4.

## 6. 의존 관계와 임계 경로

선행 관계는 §5 표의 "선행" 열이 기준이다(→는 "끝나야 시작").

| 묶음 | 선행 → 후행 |
|---|---|
| 기반 | S1.1 → {S1.2, S1.3, S1.4, S2.1} · S1.2 → S1.5(채점 대조 항목은 S1.4 → S1.5) · {S1.3, S1.5} → S1.6 |
| 입력 | {S1.4, S2.1} → S2.5 → S2.6 ← S1.3 · {S1.4, S2.1} → S2.3 |
| 출력 | {S1.4, S2.1} → S2.2 → {S2.4, S2.7} · S1.3 → S2.7 |
| 조립·평가 | {S1.2, S2.3, S2.4, S2.6, S2.7} → S2.8 → {S2.9, S2.10, S2.11} · S1.6 → S2.10 |
| 조정 | S2.9 → {S3.1, S3.2, S3.3, S3.4} · S3.1 → S3.5 · {S3.1–S3.5} → S3.6 · {S3.1, S3.2} → S3.7(S3.3–S3.5는 11/19 12:00 컷: 그때까지 결정 안 난 것은 꺼진 채 동결) |
| 마무리 | S3.7 → S4.1 → S4.2 · {S3.6, S4.1} → S4.3 |

**임계 경로**(시간 합이 가장 긴 사슬): S1.1 → S1.4 → S2.5 → S2.6 → S2.8 → S2.9 → S3.2 → S3.7 → S4.1 → S4.3 = 3 + 4 + 5 + 8 + 5 + 6 + 8 + 3 + 4 + 4 = **50h**(끝이 S4.2면 49h). S3.3–S3.5는 S3.7의 선행이 아니고 11/19 12:00 컷으로만 묶이므로 이 사슬에 들지 않는다. 입력 판정 사슬이 점수의 대부분을 정하고(R13 I1) 일정도 정한다. 출력 사슬 S2.2 → S2.4 → S2.8(9h)은 S2.5 → S2.6(13h)보다 짧아 병렬로 둔다.

**병렬 배치**: S1.1 뒤 S1.3·S1.4를 Sonnet 서브에이전트(worktree)로 돌리고 본인은 S1.2 → S1.5를 한다. C2에서는 S2.1·S2.2·S2.5·S2.9 골격과 S2.11을 Sonnet에 맡기고 본인은 S2.3 → S2.6 → S2.7 → S2.4 → S2.8을 한다. 서로 다른 파일만 만지므로 충돌이 없다. 공유 파일(`config.py`·`default.yaml`)은 슬라이스마다 자기 키만 더하고, 머지 순서는 §5 표 순서다.

## 7. 자르는 순서 (일정이 밀릴 때)

ablation부터 자른다. 자른 토글은 기본값(꺼짐)으로 남기고 ablation 표에 "미실행"으로 적는다.

1. S3.5 nano 구간·임베딩(P2)
2. S3.4 3번째 호출(P2)
3. S3.3 OOM용 KB 조각(P1)
4. S3.6 ablation 표 → 베이스라인·전체 2행 + 3회 분산만
5. S2.11 튜터 공유 API(P1) → PRD §9 제안 문서로만 남김
6. S3.1의 규칙 폭·점수 차 탐색 → 기본값(`pm4`, 3.5) 고정, ③ 정밀도와 출력 실패 기본값 측정만
7. S4.3 → 수치 표 1장 + 로그 코드 설명만

**자르지 않는 것**: 인증·200 보장·로그 규칙(S1.2, S4.1), block 범주 → decision(S2.8), safe 규칙(S2.3·S2.8), 채점 재현(S1.4·S1.5), 짝 묶음 교차 검증·홀드아웃 3회 상한, 첫 제출과 최종 제출 체크리스트(S1.5·S4.2).

## 8. 외부 미결 사항과 기본값

주최측 질문은 리더를 통해 묻는다(PRD §11-1). 답이 오기 전에는 아래 기본값으로 진행하고, 답이 오면 설정이나 `decide.py` 한 곳만 바꾼다.

| 미결(PRD §11-1) | 걸리는 곳 | 기본값 | 늦어도 |
|---|---|---|---|
| 1. 인증 실패의 기대 상태 코드 | S1.2 | 401 + `{"error": "unauthorized"}`(SPEC은 코드를 정하지 않음. 시작용 `competition/server.py`의 `HTTPException(401)`이 근거) | 11/14(C2 제출 전) |
| 2. 빈 `candidate_answer`일 때 `output` 기대값 | S2.8 | `output` 포함, `true/[]`, `safe=true`(응한 내용 없음) | 11/14 |
| 3. 비공개 거절형 답변의 `grounded`·`unsupported` 라벨 | S2.8 | block 문항은 GR-07 ①로 `true/[]`([추론] 공개 세트 block 후보 19/19가 grounded=true·unsafe, 채점은 문항 단위 `labels.output_label`을 씀, ADR-0014), 그 밖은 ②–④ | 11/19(동결 전) |
| 4. 프록시의 json_schema strict·verbosity, 동시 처리 한도, `Retry-After` | S1.3·S1.6 | `probe()`가 파라미터별로 거절된 것만 끔, 게이트 64, 재시도 1회 | 11/12 실측으로 먼저 확인 |
| 5. 과한 호출(어뷰징) 기준·하루 호출 권장치 | S2.9·S3.1–S3.6(하네스 예산) | 실행당 `eval.max_calls` 5,000(S3.6 ablation은 C3 §1의 실행별 `--max-calls`), 하루 합계 30,000회(C3 §1), 하네스 동시 실행은 채점 수준 64보다 낮게, 제출은 관문마다 | 11/16(C3 측정 전) |

R13-B2 §6의 런박스 질문(PyPI 설치 가능 여부, Python 버전)은 S1.5에서 직접 확인한다. 3.12 미만이면 네트워크가 될 때 `uv python install 3.12`, 안 되면 `requires-python`·ruff `target-version`을 런박스 버전으로 낮추고 3.12 전용 문법이 없는지 확인한다. PyPI가 막혀 있으면 미리 받은 wheel 묶음을 복사해 `--no-index --find-links`로 설치한다(C1 S1.5, §12).

## 9. 팀과 맞닿는 부분 (정보, 결정은 리더)

| PRD §9 | 가드가 내는 것 | 가드가 바라는 것 | 답이 없으면 |
|---|---|---|---|
| T1 입력 범주를 `/tutor`가 쓸지 | `Guard.classify(messages)`(S2.11), 범주 → action 표(T3) | 리더 결정 | 가드는 혼자 쓴다 |
| T2 KB 색인 공유 | `KbIndex`(S2.2) | 리더 결정 | 각자 색인 |
| T4 한 프로세스(팀 선택)·전역 게이트. 같은 런박스의 `/tutor`는 SPEC §7 요구 | `router`, `create_app()`(`/guard` + 시작용 `/tutor`), `LLM.slot()`(S1.2·S1.3) | 같은 프로세스로 둘지와 팀 서버에 `router`를 붙이는 시점 | C1·C2 제출은 `create_app()`(같은 런박스의 `/tutor` 포함) |
| T5 토큰·제출 | 제출 체크리스트(S4.2) | `TEAM_TOKEN` 값, 계정별 마지막 제출 순서 | 가드 담당 계정은 관문 제출만 |
| T6 저장소 공개 범위 | — | 해결(비공개, 11/10) | — |

## 10. 개발 규칙

- **슬라이스 = 브랜치 = PR = 이슈.** 브랜치 `gr/s2.6-input-judge`. PR 본문에 `Closes #이슈`. 저장소가 비공개로 바뀌어(PRD §9 T6) push·PR을 지금부터 한다.
- **테스트 먼저**(`ecc:tdd-workflow`). 새 모듈은 줄 커버리지 80% 이상(CI는 패키지 전체 80%). 결정적 모듈(전처리·신호·규칙·kNN·KB·decide)은 단위 테스트, LLM 단계는 `FakeLLM`으로 계약을 고정한다.
- **santa**: `prompts/input_judge.md`·`output_judge.md`(와 베이스라인 변형)는 `ecc:santa-method`로 독립 리뷰어 2명(Opus)이 모두 통과해야 머지한다. 프롬프트를 고칠 때마다 다시 한다.
- **프롬프트 버전**: 실측한 프롬프트의 문구는 그 프롬프트를 넣은 PR에서 고치지 않는다. 새 PR에서 머리말 `version`을 올려 고친다. 실측 결과는 프롬프트 `version`에 묶이고(`config_hash`), 앞 버전의 결과는 기준선으로만 쓴다(2026-11-13).
- **로그 리뷰**: PR마다 로그·예외 처리 경로를 본다. 본문·판정 값·정규화본·예외 메시지가 로그로 가는 길이 없어야 한다(GR-12). `server.py`·`llm.py`를 건드린 PR은 `ecc:security-reviewer`를 돌린다.
- **홀드아웃**은 S3.7 전에는 보지 않는다. 열람은 최대 3회이고 하네스가 센다. 기준값·프롬프트는 교차 검증으로만 정한다.
- **개선 판정**: 같은 빌드 3회 분산 안의 차이는 개선으로 보지 않는다. 채택 기준은 하나다: 3회 분산 밖에서 순이득 > 0. "분산 밖"은 후보의 3회 범위와 기준의 3회 범위가 **겹치지 않는다**는 뜻이다(차이가 없을 때 거짓 채택 5%, R14 §2 A1). 동결 전 3회 확정 실험은 입력 1회·출력 1회·예비 1회가 상한이고 시도한 변형 수를 `results/`에 적는다(R14-D7). 출력 ③의 켜기와 `tier3_trust`는 총점 범위가 아니라 저장된 응답의 정밀도와 건수로 정한다(GR-07, R14-D2). 묶음 단위 부트스트랩 구간은 보고하되 채택 기준이 아니다(R13 D10, PRD §7).
- **하드코딩 금지**: 질문 → 답 표, 요청 내용 캐시, 채점 요청을 알아보는 분기를 두지 않는다. 색인·LR은 기동 때 배포 자료로만 만든다(GR-14).
- **보안**: 키·토큰은 환경변수와 `.env`(git 제외). 테스트 픽스처는 공개 세트에서만 가져온다.
- **결정이 갈리면** `/ecc:council`로 정리하고, 사용자 몫이면 묻는다. 설계가 바뀌면 ADR을 고친다.
- **계약 대조**: Phase 끝마다 15분을 써서 §4 계약과 실제 코드(서명·설정 키·CLI)를 맞춘다. 다르면 이 문서를 먼저 고친다.
- **동결 뒤**(11/20 18:00): 형식 오류·시간 초과·로그 규칙 위반을 고치는 PR과 팀 서버에 가드 라우터를 붙이는 연결 코드(판정 로직 변경 없음)만 넣는다. 각 PR은 교차 검증 1회와 부하 시험으로 확인한다.
- **문서**: 슬라이스가 끝나면 Phase 문서 체크박스와 에픽 체크리스트를 갱신한다.

## 11. 진행 추적 (GitHub)

| 항목 | 규칙 |
|---|---|
| 에픽 | Phase마다 1개: `[C1] 기반·첫 제출` … `[C4] 마무리`. 본문 = 슬라이스 체크리스트 + Phase 문서 링크 |
| 이슈 | 슬라이스마다 1개, 그 Phase가 시작될 때 만든다(C1은 11/11 아침). 제목 `[S2.6] 입력 판정 nano·프롬프트 v1 (GR-04)`. 본문은 Phase 문서의 해당 절. 에픽의 하위 이슈로 잇는다 |
| 라벨 | 가드 트랙: `guardrail`(전부) · `guardrail:epic` · `guardrail:P0`/`P1`/`P2` · `guardrail:blocked`(주최측·팀 답 대기). 공용: `enhancement`(기능 슬라이스), `documentation`(문서 PR), `bug`(동결 뒤 수정), `question`(§8 미결). 팀 서버·제출에 닿는 슬라이스(S1.5·S2.10·S2.11·S4.2)는 `orchestrator`도 단다 |
| 번호(11/10 생성) | 에픽 C1 #42 · C2 #43 · C3 #44 · C4 #45. C1 슬라이스 #46–#51(S1.1–S1.6, #42의 하위 이슈). C2–C4 슬라이스는 Phase 시작 때 |
| v4 정리(11/10 완료) | v4 에픽 #2–#6(GP0–GP4)과 슬라이스 이슈 #15–#21을 "v6 계획으로 대체" 댓글과 새 에픽 링크를 달아 닫았다(not planned) |
| CI | `.github/workflows/guardrail-ci.yml`은 작업 하나 `guardrail / gate`: `uv sync --locked` → `ruff check` → `ruff format --check` → `pytest --cov --cov-fail-under=80`. gitleaks는 `integration / gate`에서 저장소 전체를 돈다. `live` 테스트는 CI에서 돌지 않는다 |
| 담당 | danielcho02 |

## 12. 리스크

| 리스크 | 가능성 | 대응 |
|---|---|---|
| 공개 200행 과적합(비공개는 겹치지 않음, 변형 샘플링) | 높음 | 짝 묶음 교차 검증, 홀드아웃 3회 상한, 규칙은 가드·튜터 근거 있음(G∪T)까지 넣은 정밀도로 채택(PRD §10) |
| 짝 문항에서 nano가 범주를 뒤집음 | 높음 | 짝째 few-shot, 반대쪽 최근접 예시 필수, 짝 문항 정확도 따로 보고, S3.2 집중 |
| 규칙 ②의 오탐(계산 결과가 든 근거 있음 답변) | 중간 | `NUM_RX` 토큰만(값 정규화는 ablation), 점수 차 조건, 튜터 답변으로 추가 확인 |
| 비공개 거절형 변형의 grounded 라벨이 다름 | 낮음 | 채점은 문항 단위 `output_label`이라 block 문항은 ①로 `true/[]`, §8 미결 3 |
| nano "근거 없음" 정밀도 0.5 미만 | 중간 | ③을 끈다(`nano_tier3: false`, S3.1). 출력 호출은 v6 모드에서 정밀도 기록용으로 남는다(`output.call_nano_when_unused`가 true인 동안. 동결 때 false로 낮출 수 있다) |
| 튜터와 같은 키 동시 호출 → 429·지연 | 중간 | 전역 게이트, 요청 예산, 기본값(GR-09·10), S2.10 부하 시험 |
| 로그·예외가 본문을 남겨 실격 | 낮음 | S1.2 미들웨어, PR마다 로그 리뷰, S4.1 grep |
| 과한 호출로 키 회수 | 중간(C3 ablation이 이틀에 약 22,400회, 기준은 §8 미결 5) | 하네스 `--max-calls`, 하루 합계 30,000회(C3 §1), 부하 시험은 채점 수준까지, 제출은 관문마다 |
| **C2 과부하**(3.5일에 50h) | 높음 | Sonnet 병렬 20h, §7 순서, 11/15 밤 관문 못 넘으면 C3 첫날 사용, 동결일 고정 |
| 런박스 환경(설치 불가, Python <3.12, 세션 2일 제한) | 중간 | S1.5에서 먼저 확인(대안: `uv python install 3.12` / `requires-python` 낮춤 / wheel 묶음 복사, §8), `TEAM_TOKEN`은 `[A-Za-z0-9_-]+`(셸 `. .env`로 읽음), 재시작 절차를 C1 문서에 적음, 최종 제출 전 재시작(S4.2) |
| 팀 서버 통합 지연·충돌 | 중간 | `router`와 `create_app()` 둘 다 유지, 통합은 리더 일정에 맞춤, 동결 뒤에는 붙이는 연결 코드(판정 로직 변경 없음)만 |
| 마지막 제출이 서버 문제로 낮게 나옴 | 낮음 | 11/24 18:00까지 제출, 11/25 아침에는 상태 확인만, 제출 전 부하 시험 |

## 13. 변경 기록

**v23 (2026-11-10, PRD v6 전환)**: SPEC.md·공개 세트·채점 코드를 받고 PRD v6(R13 결정 D1–D10, ADR-0012–0014)에 맞춰 전부 다시 썼다. "v4 기준" 절은 남기지 않았다.
- 구조: 튜터 래퍼(`Guard.respond`, 13단계, trace)를 버리고 상태 없는 판정기 `Guard.judge()` 하나로. nano 2회 병렬(입력 범주·출력 근거), decision은 코드, 출력 3단, safe 규칙, kNN·LR은 예시·기본값.
- 모듈: `pipeline.py`·`trace.py`·`types.py`·v4 `eval/*` 삭제, `llm.py`·`config.py`·`preprocess.py` 고쳐 씀(정규화본은 NFKC 없이 유지, 골격 뷰 삭제, 붙인 뷰는 구현하지 않음), `server.py`·`schema.py`·`guard.py`·`decide.py`·`signals.py`·`rules.py`·`knn.py`·`classifier.py`·`kb.py`·`data.py`·`judges/*`·`eval/{score,cv,run,load,probe}.py` 신설.
- 계약: §4 전부 새로(타입, 판정 흐름, LLM 호출, GR-11 토글 11개를 포함한 설정, 짝 묶음 분할 파일, 평가 CLI, 의존성).
- 일정: Phase GP0–GP4 → C1–C4(11/11–11/24), 동결 11/20, 슬라이스 27개(111h, P0 94h), 임계 경로 50h.
- 추적: C1 에픽·이슈 #42–#51을 11/10에 만들고, v4 에픽 #2–#6과 이슈 #15–#21을 같은 날 닫았다(§11). 라벨에 공용 `enhancement`·`documentation`·`bug`·`question`과 `orchestrator`를 함께 쓴다.
- santa 4라운드(같은 날): 규칙 ② 기본을 V2 + 점수 차 게이트로, 규칙 정밀도 P에 따른 `rule_emit_spans`, 신호별 뷰(붙인 뷰는 구현하지 않음), 파싱 실패 기본값 통일, `create_app()`의 `/tutor` 인증, 교차 검증 행 수(약 160 + 홀드아웃 약 40)를 반영했다(R13-A §6).
- v1–v22의 기록(santa·council 반영 내역 포함)은 커밋 `50d6142`의 이 문서 §13에 있다.

**v23 수정 (2026-11-13, R14 council 3라운드, 사용자 채택)**: [R14 §6](research/R14-prompt-uplift.md#6-council-2026-11-13-후보-선별)의 R14-D1–D8을 반영했다.
- §4.1 `Judgement`에 `answer_quote_ok`·`kb_quote_ok`·`nano_refuses`. §4.3 `OutputOut`을 `answer_quote → kb_quote → grounded → refuses`로(`reason` 삭제). §4.4 `output.tier3_trust`·`quote_min_chars`·`context_max_chars`. §4.2 출력 nano 문맥을 1위 섹션 전체 + 다른 섹션 조각으로.
- §4.6 `--input-only`와 역할별 실패율 2% 관문. §5 S2.7·S2.9·S3.1·S3.2 수용 기준, S3.4는 동결 전 미실행. §10 "분산 밖"의 뜻과 확정 실험 상한.
- 바뀌지 않은 것: 입력 프롬프트 v1 문구, 범주 → decision(ADR-0012), safe 규칙(D6), 키워드 힌트 금지(GR-04).
- S2.9 결정(같은 날, 3회 실측과 council): `llm.max_completion_tokens`를 800 → 1600으로, `toggles.nano_span`을 켬으로 바꿨다(설정 `version` 6.0.2). 근거는 [results/2026-11-13-v1.md](../results/2026-11-13-v1.md): 출력 호출 잘림 6.2–9.4% → 0/192(조합 3회), 대신 `read` 12초 시간 초과가 유효 6회 8/384(2.1%), 출력 평균 0.827–0.837 → 0.889–0.899. `nano_span`은 가드 점수 범위로는 겹쳤지만(입력 잡음이 block F1 0.04–0.09) 출력 단독에서 같은 응답에 조립만 바꾼 값이 800·1600 모두 겹치지 않았다(ADR-0014 S2.9 수정). `knn_fewshot`은 그대로 켠다: 입력 단독 실측의 "예시 없음이 낫다"는 결과가 재현되지 않았다. ablation 파일 `with_nano_span`은 `no_nano_span`으로 바꿨다.
- S2.8 santa 3라운드(같은 날): ① 출력 쪽 준비(KB 검색·규칙 ②)가 실패해도 입력 판정은 그대로 한다(GR-09 "출력 실패는 출력만 바꾼다"). 출력을 못 만들 때의 safe는 GR-08을 따른다(숨긴 태그·block 순응이면 false). ② 프롬프트 파일은 `Guard`를 만들 때 읽는다(경로가 틀리면 기동에서 실패하고 서버 기본 판정기는 `reason=FileNotFoundError`로 내린다). ③ 없는 설정 목록에 `rule_extra_tokens`의 `bare_numeral`을 더했고, 하네스는 그런 설정 행이 있으면 호출 전에 사용법 오류로 끝낸다(`--only`에서 빼야 한다). ④ 인증이 아닌 probe 실패는 기동을 막지 않되 `guard judge=real probe=<오류 코드>`를 오류 수준으로 남긴다.
- S2.8 santa 2라운드(같은 날): 아직 구현하지 않은 설정(`third_call`·`oom_kb_snippet`·`embedding`·`output.rule_require_same_kind`)을 켜면 `Guard`가 만들어질 때 `NotImplementedError`로 실패한다(조용히 무시하지 않는다. 하네스는 멈추고, 서버 기본 판정기는 상수 판정기 + `reason=NotImplementedError` 로그로 내린다). 하네스의 예상 호출 수는 상한이다(저신호 직접 판정 행만큼 덜 부른다).
- S2.8 조립 중 조정(같은 날, santa 1라운드와 council 2라운드): 서버 기본 판정기를 키 기준(`default_factory`)으로 정했다. 키가 있으면 `Guard.build`, 없거나 기동이 실패하면 상수 판정기 + 기동 로그 한 줄이다. 무조건 `Guard.build`로 바꾸면 통합 테스트 464개 중 154개가 깨지고, 상수로 두면 통합 앱의 `/guard`가 상수 판정으로 제출된다. `candidate_answer`가 문자열이 아닐 때는 GR-07 표를 그대로 탄다(block 범주면 ①). 학습자 발화가 없어도 숨긴 태그는 본다. `Judgement.errors` 코드 목록을 §4.1에 적었다.
- S2.8 조립 전 조정(같은 날, 출력 단독 실측 뒤): §4.4 `nano_span`의 뜻을 "③에서 확인된 `answer_quote`를 구간으로"로 적고 허용 조건을 `nano_tier3`로 바꿨다. §4.1 인용 확인의 답변 쪽 대조 대상을 자른 원문으로 못박았다. 근거는 [results/2026-11-13-output-v1-solo.md](../results/2026-11-13-output-v1-solo.md)(인용을 구간으로 내면 출력 평균 0.858–0.869 → 0.892–0.909). 기본값은 끔 그대로다.
- 첫 실측 뒤 조정(같은 날, 사용자 결정): 입력 v1 단독 실측의 block F1(가드 160행, 1회 0.771)이 목표 0.90에 못 미쳤다. S2.7 전에 v1을 2회 더 돌려 3회 범위를 만든다(§4.6). 입력 프롬프트 v2는 S3.2의 새 PR에서 한다(§5 S3.2, §10 프롬프트 버전).
- 영어 지시문 실험(2026-11-14, 사용자 결정 + council, R14-D9): 판정 프롬프트 3개의 지시문을 영어로 옮긴 `prompts/*_en.md`(`version: "1.1"`)를 더한다. 실험 시작 때 기본 설정은 한국어 v1이었고 영어판은 `--set`으로만 골랐다(아래 채택 항목으로 바뀜). 같은 코드로 한국어·영어를 번갈아 3회씩 재서 R14-D9 기준을 넘을 때만 기본값을 바꾼다. 측정 순서는 S3.2 다음이다. 숨긴 문자 표식과 JSON 모드 안내 문장은 튜터와 같은 계약이라 한국어로 둔다.
- 영어 지시문판 채택(2026-11-14, R14-D9 기준 통과, [결과](../results/2026-11-14-v1-english.md)): 기본 설정의 입력·출력 판정 프롬프트를 `input_judge_en.md`·`output_judge_en.md`(1.1)로 바꾸고 설정 `version`을 6.0.3으로 올린다. 같은 코드 번갈아 3회에서 가드 84.87–88.33(한국어 81.93–87.59), 출력 평균은 분산 밖으로 올랐다. 베이스라인 설정의 입력 프롬프트도 기본과 같은 영어판으로 바꾼다("예시만 뺀 같은 프롬프트" 불변식). `output_judge_baseline`은 영어로 재지 않아 한국어 v1 그대로 둔다. 베이스라인 수치는 다시 재야 한다. 측정 순서는 사용자 결정으로 S3.2보다 앞섰다.
