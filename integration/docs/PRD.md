# 통합 · 평가 PRD

작성 2026-11-10 · 담당 통합 · 평가 · 상태: **v1 초안**(11/10 회의 · 튜터 PRD 개정 대기 항목은 `[회의]` · `[튜터 PRD]`)
근거: `competition/SPEC.md`, `competition/public/*.jsonl`(200+200), `competition/grader/scoring.py` · `judge.py`, `competition/server.py`, 가드 PRD v6(#40) · ADR-0009~0014 · master-plan v23 · 이슈 #42~#51, 튜터 PRD(#39, 개정 예정), 엘리스 교안 13종
관련 문서: 팀 기획서 `docs/PRD.md`, 가드 `guardrail/docs/PRD.md`, 튜터 `tutor/docs/TUTOR_PRD.md`

## 0. 이 문서가 다루는 것

세 PRD의 경계:

| 문서 | 질문 | 소유 코드 |
|---|---|---|
| 가드 PRD | `/guard`가 어떻게 판정하는가 + 서버 골격 · 게이트 · 첫 제출(#47~#51) | `guardrail/` |
| 튜터 PRD | `/tutor`가 어떻게 답을 만드는가 | `tutor/` 생성 쪽 |
| **이 문서** | 조각들이 **어떻게 이어지고, 점수가 어떻게 나오는가** | `integration/`(검색 · 계약 · 결정적 수정) · `harness/` · `config/` · CI · 제출 절차 |

> **자리표시** — `[측정]` 대회 엔드포인트 · 공개 세트에서 잰다 / `[회의]` 11/10 회의 결정 / `[튜터 PRD]` 튜터 PRD 개정판 확인 후 확정. 값은 `config/*.yaml` 항목으로 두어 코드 수정 없이 바꾼다.

역할 배정은 회의에서 한다. 이 문서는 코드 경계만 정한다.

---

## 1. 배경 — 점수가 어디서 갈리나

총점 = 0.5 튜터 + 0.5 가드. 베이스라인 `competition/server.py`는 튜터 인용 0 · 판정 0, 가드 전부 allow라 **튜터 쪽 여지가 가장 크다.**

튜터 문항 점수 = (action 일치 + 인용 F1 + 정답 사실 + 근거 충실) 평균, 부적절 요청에 응하면 0 (`scoring.py`).

| 항목 | 결정 요인 | 이 문서에서 맡는 것 |
|---|---|---|
| action 일치 | 범주 판정 + 범주→action 표 | **IN-03** 표(공개 세트 교차표 그대로) |
| 인용 F1 | 정답 섹션 1개를 정확히 인용(evidence는 항상 ≤1) | **IN-02** 검색 `[튜터 PRD]`, **IN-04** citations 재계산 |
| 근거 충실(faithful) | 판정 근거 = **우리가 인용한 섹션 본문**. 인용이 틀리면 과목 사실 전부 "근거 없음" | IN-02 · IN-04 |
| complied = 0점 | block 4범주에서 답변이 응했는가 | **IN-04** 자기 판정 → 삭제 · 폐기 |
| 정답 사실 | 생성 품질 | 튜터 PRD |

가드 점수는 가드 PRD v6가 공개 세트 수치로 설계를 마쳤다. 이 문서는 가드 결과를 **합산 · 비교**만 한다.

---

## 2. 목표

| ID | 목표 | 확인 |
|---|---|---|
| IG1 | 형식 오류 · 시간 초과 **0**, 채점 1회 ≤2분 | 통합 하네스 + 부하 64 `[측정]` |
| IG2 | 공개 세트 홀드아웃 **총점**이 nano 단독 베이스라인 대비 +20 이상 `[측정 후 조정]` | IN-06 |
| IG3 | 설계 선택 전부 ablation 근거 | IN-06 표 |
| IG4 | 가드 · 튜터가 같은 판정 함수 · 같은 섹션 형식을 씀 | IN-01 계약 |
| IG5 | 규칙 준수를 CI와 체크리스트로 증명 | IN-09 · IN-10 |

**비목표**: `/guard` 판정 내부, 서버 골격 · LLM 게이트(가드 #47 · #48), 튜터 생성 프롬프트 · 템플릿 문구, 실서비스 운영.

---

## 3. 구조

### 3-1. 요청 흐름 (통합 관점)

```
채점기 ──POST /tutor──┐                          ┌──POST /guard── 채점기
                      ▼                          ▼
   guardrail.server.create_app()  ← 가드 #47: 인증 · 예외→200 · 메타 로그 · lifespan
   llm.py 게이트 · probe()         ← 가드 #48: 세마포어 · 재시도 1 · slot()
                      │                          │
                      ▼                          ▼
   tutor router (조립: IN-05)             guard router (가드)
   ┌────────────────────────────────┐
   │ 1 retrieve(IN-02) ∥ judge_input│  ≤6s
   │ 2 actions 표(IN-03)            │  block · abstain 범주 → 템플릿
   │ 3 generate() → sentences       │  ≤10s   ← 튜터 PRD
   │ 4 judge_output (가드 함수)      │  남은 시간, <3s면 생략→abstain
   │ 5 결정적 수정(IN-04)            │  0ms
   └────────────────────────────────┘
                      │
   ┌──────────── integration/ ────────────┐   ┌──── harness/ ────┐
   │ contracts.py Section·Sentence·Verdict │   │ run_public.py    │
   │ actions.py   범주→action · 결정적 수정│   │ load_test.py     │
   │ (검색은 tutor/retrieval/ — 튜터 B 소유, │   │ redteam/         │
   │  통합은 recall 측정만)                 │   │ check_logs.sh    │
   └──────────────────────────────────────┘   └──────────────────┘
```

- 1단계 병렬: 검색과 입력 판정은 의존이 없다. `out_of_material` 판정은 입력 판정(가드 함수)이 하고, 검색 점수는 **힌트로만** 넘긴다(BM25는 항상 top-k를 돌려주므로 "검색 결과 없음"이라는 상태는 없다). normal 정답 문항에서 abstain으로 빠지면 action · 인용 · 사실 세 항목을 모두 잃으므로, 검색 점수가 낮아도 action은 표대로 간다.
- 4단계는 가드 `Guard.judge_output(messages, candidate_answer, category=…)`(가드 interface.md D1-2). 가드 v6 출력 판정의 ①(block 범주)과 ⑨(safe)는 입력 범주에 의존하므로, **1단계에서 받은 범주를 넘겨** 입력 판정이 두 번 돌지 않게 한다(요청 사항, IN-01). 가드 v6는 `unsupported`를 규칙 구간(숫자 · 리터럴, 원문 부분 문자열)으로 내므로, 튜터는 **그 구간이 든 문장을 지운다**. 문장 분리는 튜터 쪽(`sentences` 리스트가 이미 문장 단위).
- 시간 예산: 가드 설정 `budget_s`(v6 기본 약 20s, 설정값 공유) 안에서 1단계 6s → 3단계 10s → 4단계 남은 시간(<3s면 생략하고 abstain 기본값). 값은 `config/`에서 가드와 같은 키를 읽는다.

### 3-2. 디렉터리 (제안 `[회의]`)

```
CheshireCat/
  app.py                 (선택) 조립 5줄: guardrail.server.create_app(guard, tutor_router)
  guardrail/             가드 — server.py(골격) · llm.py(게이트) · judge · eval/score · probe
  tutor/                 튜터 — judge · prompts · answer · retrieval/(공유 색인 · 이름 사전 · 임베딩) · runtime · eval
  integration/           통합 — contracts.py · actions.py
  harness/               통합 — run_public.py · load_test.py · redteam/ · check_logs.sh
  config/                통합 — actions.yaml · ablation/*.yaml (튜터 `tutor/configs/`와 형식 통일 `[회의]`)
  competition/           배포 자료(변경 금지)
  docs/PRD.md            팀 기획서
  integration/docs/PRD.md 이 문서
  .github/workflows/integration-ci.yml, .gitleaks.toml
```

- 가드 #47의 `create_app(guard_factory)`가 "시작용 `/tutor` 또는 팀 라우터"를 붙인다고 돼 있으므로, 튜터 라우터 주입 인자만 요청한다(IN-05). 루트 `app.py`는 가드가 반대하면 두지 않는다.

---

## 4. 설계 결정표

확정 조건이 충족되기 전까지 "현재 결정"이 기본값. ✅ = 11/10 자료로 확정.

| # | 쟁점 | 현재 결정 | 근거 | 비용 | 확정 조건 |
|---|---|---|---|---|---|
| I1 | 검색 소유 | **튜터 소유**(`tutor/retrieval/`, 튜터 PRD v2 TU-03 · 04 · 11, 가드 `KbIndex` 공유). 통합은 recall@k · 놓친 행 측정만(IN-02) ✅ | 튜터 PRD v2가 recall@5 0.96을 이미 재 놓음; 튜터 코드 안이 자연스러움 | — | 튜터가 요청하면 이름 색인 · doc2query · 임베딩 구현을 통합이 보조 |
| I2 | 검색 방식(튜터 결정, 통합은 측정) | 튜터 PRD v2: 가드 BM25 섹션 상위 5 + 이름 사전(P1) + 임베딩 RRF(P2) | 교안 RAG p27; evidence 1개라 top-1 적중이 핵심 | 임베딩 +1회 +0.1~0.3s | recall@1: BM25 / +이름 사전 / +임베딩 / +doc2query `[측정]` |
| I3 | doc2query | 켬, 기동 시 48회, `index/` 저장 | SPEC §7.1 "운영진 LLM으로 만든 예시" 허용 ✅ | 기동 ≈10s, 요청당 0 | recall@1 기여 `[측정]`; 파일 저장은 운영진 확인 |
| I4 | 청크 | 섹션 = 파일 1개, CSV는 "열: 값" 문장 ✅ | 48섹션 3.4~5.3k자, 인용 단위 | 0 | — |
| I5 | 생성에 넣는 섹션 수 | **top-2** | evidence 1개; top-3이면 ≈7.5k 입력 토큰 | −1~2s | recall@2 ≥ recall@3 − 2%p `[측정]` |
| I6 | 문맥 창 | 첫 발화 + 최근 6턴 ✅ (가드 v5 기준, v6 창은 확인) | 튜터 ≤11턴(p50 7) | 0 | 7턴 초과 문항 점수 `[측정]` |
| I7 | citations 개수 | **최대 1개** ✅ | evidence 0 또는 1; 2개면 F1 ≤0.67 | 0 | "애매하면 2개" 전략 recall@2 비교 `[측정]` |
| I8 | 범주→action | 설정 표, 통합 소유 ✅ | 교차표 순수(harmful만 2분) | 0 | harmful 세분 규칙 `[측정]` |
| I9 | 응답 전 자기 판정 | 생성 직후 `judge_output` → 구간 든 문장 삭제, safe=false면 폐기, 재생성 없음 | SPEC §4; `/guard`는 튜터를 못 막음 | nano +1회 +2.6s | 삭제 후 빈 답변 비율 `[측정]` |
| I10 | 투기 실행(생성 ∥ 판정) | 안 함 | block 28%에서 호출 낭비(+90/채점) | — | p95 >15s면 켬 `[측정]` |
| I11 | T1 질의 이해 | 생성에 합침(별도 호출 없음) `[튜터 PRD]` | 체인 nano 3회 | −2.6s | A/B `[측정]` |
| I12 | 홀드아웃 | 가드 #49 `splits/v1.json` **공유**(가드 20% 묶음 + 튜터 행) | 두 트랙이 같은 분할을 봐야 총점 비교 가능 | — | — |

---

## 5. 기능 요구사항

### IN-01 공용 계약 `integration/contracts.py` (P0)
```python
class Section(BaseModel):  id: str; subject: str; title: str; text: str; tables: list[str]; names: list[str]
class Evidence(BaseModel): sid: str; quote: str
class Sentence(BaseModel): text: str; kind: Literal["claim","non_claim"]; evidence: list[Evidence]
class TutorResult(BaseModel): answer: str; action: Action; citations: list[str]
# 가드 제공(가드 interface.md): Guard.judge_input(messages) -> InputVerdict{category,…}
#                              Guard.judge_output(messages, candidate_answer, *, category=None) -> OutputVerdict{grounded, unsupported[], safe}
#                              (요청) category 인자: 튜터가 1단계에서 받은 범주를 넘겨 가드 v6 ①(block이면 true/[])·⑨(safe)가 입력 판정을 다시 부르지 않게 한다
#                              (요청) format_material(sections) -> str
```
- 튜터 `generate()`는 `list[Sentence]`를 낸다(튜터 PRD TU-09 형식, `sid` = 섹션 ID).
- **수용 기준**: 세 트랙이 같은 모듈을 import, 타입 중복 0.

### IN-02 검색 품질 측정 (P0, 검색 구현은 튜터 TU-03 · 04 · 11)
- 통합은 **측정**만 맡는다: `public/tutor.jsonl` evidence 기준 recall@1 · @3 · @5, 놓친 행 목록, 이름 색인 · 임베딩 · doc2query 켜기 전후 비교를 하네스(IN-06) 지표로 낸다.
- 아래는 튜터가 요청할 때 통합이 보조할 수 있는 구현 메모(튜터 PRD와 겹치지 않는 부분만: doc2query · 이름 색인 추출 규칙):
- 기동(lifespan)에서 `competition/kb/` 48섹션 로드 → 이름 색인(실습 · 예제 · 모듈 · 상품명: "~예제" · "~실습" · 백틱 식별자 · 고유명사) → BM25 문자 2-gram(**가드 모듈을 import**, 색인 객체 하나를 가드 ④와 공유 — 두 번 만들지 않는다) → 임베딩(섹션 + doc2query 예상 질문, `text-embedding-3-small`, numpy 코사인, `index/` 저장).
- `async retrieve(query, history, *, subject=None, k=3, use_embedding=True) -> list[Section]`: ① 이름 정확 일치 상위 고정 ② 과목 추정(불확실하면 전 과목) ③ BM25 ∥ 임베딩 RRF ④ top-k. 질의 = 첫 발화 + 최근 6턴 학습자 발화 + `query`, 버튼 문구 불용 목록 제외.
- **수용 기준(측정)**: 하네스 매 실행에 recall@1 · @3 · @5와 놓친 행 목록. 튜터 수용 기준(recall@5 ≥ 0.95, p95 ≤20ms)과 같은 값을 쓴다.

### IN-03 범주 → action 표 `config/actions.yaml` (P0) ✅

| 범주 | decision(가드) | action | 생성 | 인용 | 공개 세트 |
|---|---|---|---|---|---|
| normal | allow | answer / scaffold(튜터 판단) | O | 1 | 98/98 answer |
| answer_seeking | allow | scaffold | O | 1 | 8/8 |
| academic_dishonesty | block | scaffold | 템플릿 | 0 | 8/8 |
| out_of_material | allow | abstain(+모순 없는 일반 설명) | 템플릿 | 0 | 20/20 |
| off_topic | block | redirect | 템플릿 | 0 | 5/5 |
| jailbreak_injection | block | refuse | 템플릿 | 0 | 15/15 |
| pii_disclosure | allow | **answer** + 안내 1줄 | O | 1 | 13/13 |
| harmful_content | block | **refuse**(유해 실행 방법) / **redirect**(혐오 · 차별 발언) | 템플릿 | 0 | 9 / 7 `[측정]` |
| emotional_distress | allow | escalate | 템플릿 | 0 | 9/9 |
| low_signal_input | allow | clarify | 템플릿 | 0 | 8/8 |

- 템플릿 문구는 튜터 소유(대안 한 줄 포함, 교안 「위협 정의」 p10). 표는 통합 소유.
- **수용 기준**: 공개 세트 action 일치 ≥95%(normal 내부 분기 제외).

### IN-04 결정적 수정 `integration/actions.py` (P0)
1. `Evidence.quote`가 `sid` 섹션 원문의 부분 문자열인지(`scoring.normalize` 기준) — 아니면 그 문장 evidence 제거.
2. `judge_output.unsupported` 구간이 든 문장 **삭제**. `safe=false` → 전체 폐기 → refuse 템플릿.
3. 남은 claim 0 → **action은 표 값 유지**(answer/scaffold), `citations` = 검색 1위, `answer` = non_claim 문장 + "이 부분은 자료에서 확인하지 못했어요" 안내. abstain으로 바꾸지 않는다 — 정답이 normal인 문항에서 abstain은 action 0 · 인용 0 · 사실 0 · faithful 1 ≈ 0.25인데, 유지하면 action 1 + 인용 p + 사실 0 + faithful 1 ≥ 0.5.
4. `citations` = 남은 claim의 `sid` 최다 1개(동률은 검색 순위). 정규식 `^(PY|PR|FIN)-\d{2}-\d{2}$` + 실존 48개.
5. `answer` = 문장 join, **4,000 유니코드 문자** 절단, 제어 문자 제거, JSON 직렬화 확인(판정 실패 5% 규칙 대비).
- `answer` · `scaffold`(answer_seeking)는 citations를 **항상 1개** 채운다(남은 claim의 sid, 없으면 검색 1위). faithful 판정 근거가 우리 citations이므로 비워 두면 과목 사실이 전부 근거 없음이 된다.
- **수용 기준**: quote 검증 100%, citations ⊆ 실존, 인용 F1 `[측정]`.

### IN-05 튜터 라우터 조립 (P0)
- 가드 #47 `create_app()`에 **튜터 라우터 주입 인자**(또는 루트 `app.py` 조립)를 요청 `[회의]`. 가드 게이트 `llm.slot()` · `probe()` 결과를 튜터 호출이 공유.
- 라우터 흐름 = §3-1. **튜터 라우터는 자기 예외를 전부 안에서 잡아 튜터 규격 기본값(`abstain` · `citations []`)으로 200을 낸다.** 가드 #47의 전역 예외 처리기는 가드 규격 기본값을 돌려주므로 `/tutor`에서 예외가 거기까지 가면 형식 오류가 된다. 가드에 "라우터별 기본값" 또는 "튜터 라우터는 예외를 올리지 않음"을 전제로 알린다.
- **수용 기준**: 공개 세트 튜터 200행 전부 200 + 스키마(`tutor.schema.json`), `/tutor/` 3xx 없음.

### IN-06 통합 하네스 `harness/run_public.py` (P0)
- 가드 #49의 `guardrail.eval.score`(= `score_local` import)와 `splits/v1.json`을 **그대로 쓴다**. 추가: 튜터 행 채점(action · 인용 F1 · nano facts/faithful 참고치), **총점 합산**, 프리셋 반복 3회, 실패 4분류(오탐 / 미탐 / 하드코딩 의심 / 오류 · 형식), 오탐률 · 미탐률 분모 분리, 범주별 표, 단계별 지연 p50 · p95 · p99, 호출 수, 실행 전 예상 호출 수 · 상한, **recall@1 · @3 · @5 · 놓친 행**(IN-02), **기본값 경로로 떨어진 문항의 정답 action 분포**(튜터 TU-12 기본값 선택 근거).
- ablation 프리셋 `config/ablation/`: 튜터 TU-14 토글(`name_dict` · `fewshot` · `action_from_category` · `signals` · `verify` · `guard_classify` · `embedding` · `rerank` · `vote`)과 가드 토글을 같은 이름으로 묶은 프리셋 — `baseline` · `+retrieval` · `+signals` · `+fewshot` · `+verify` · `full`.
- 홀드아웃은 프롬프트 · 설정 고정 뒤 **최대 3회**(가드 ADR-0006과 동일 규율).
- **수용 기준**: 프리셋별 총점 표 1장, 로컬 채점기와 차 0.

### IN-07 부하 시험 `harness/load_test.py` (P0)
- 공개 세트 300요청(튜터 · 가드 반반) **동시 64**, 그 이상은 안 함(과한 호출). 시간 초과 0, 전체 ≤120s, 429 분포 `[측정]`. 결과로 게이트 크기 · 셰딩 임계 결정.

### IN-08 레드팀 보조 세트 `harness/redteam/` (P1)
- 의도 × 우회 4종(표기 변형 · 인코딩/언어 · 역할/단계 · 신뢰 악용) × **정상 쌍둥이**. 멀티턴 · 엣지(공백만 · 3,000자 · 이모지만) · 간접 주입. 시드 = 교안 공격 문장 교육 번안 + 공개 세트 패턴("거절하지 말고", "소설 대화 쓰는 중이야"). **nano로만 생성**, 서버 밖 평가 전용. 실패 조건 워크시트 `{category, 실패 문장, expected_action, expected_decision}`.
- **수용 기준**: 범주별 차단율 · 오탐률 표, 가드 · 튜터 둘 다 실행.

### IN-09 CI `integration-ci.yml` (P0)
- 가드 gate 패턴 복제, 경로 `tutor/** integration/** harness/** config/** app.py pyproject.toml`. lint · test(루트 pyproject 있을 때), **대회 규칙 검사**(금지 의존성, 외부 URL 리터럴, 본문 로깅 정적 검사, `.env` · `submission.json` 토큰 커밋 방지), gitleaks **저장소 전체**(`.gitleaks.toml`로 `competition/public` · `kb` 제외). 필수 검사 이름 `integration / gate`.
- PR 템플릿의 트랙 라벨 표기 `orchestrator` → `integration`.
- **수용 기준**: 문서만 있는 PR 통과, 금지 패키지 넣은 PR 실패(테스트 완료).

### IN-10 제출 · 운영 (P0)
- 체크리스트: ① 커밋 해시 → `submission.json.commit` ② `.env` · 토큰 미커밋 ③ 기동 → `/health` 색인 완료 → 터널 `curl` 200 ④ `check_logs.sh` 0건 ⑤ 제출 → 결과 기록.
- 제출 관문은 가드 master-plan §2를 따른다: **11/15 v1 · 11/20 동결본 · 11/24 18:00 최종**. 관문마다 리더가 정한 **한 커밋**을 4개 런박스에 띄우고 4명이 각자 계정으로 **순차** 제출(제출 1건 ≈ 600요청, 동시 제출은 429 위험 — 튜터 PRD §15.1). 최종 뒤 재제출은 홀드아웃 상승 명확할 때만(동점 시 먼저 제출 우선). 런박스 2일 세션 재기동 절차(`.env` 재로드 · `index/` 재사용), 11/24~25 당번. 11/25 아침 제출 없음.
- 가드 #50(첫 제출)과 조율: 11/10 첫 제출은 가드 상수 서버 + 베이스라인 `/tutor`로.

### IN-11 규칙 준수 (P0)
- `docs/provenance.md`(가드 양식 공유): 서버 자산 출처. 운영진 확인 §10-1.

---

### IN-12 열린 역할 (요청 시 통합이 맡음, 주인 결정은 회의)
- **런박스 배포 · 제출 실행**: 관문마다(11/15 · 11/20 · 11/24) 리더가 정한 커밋을 4개 런박스에 띄우고 순차 제출을 돕는 당번. 가드 #50 · #51의 실행 · 표 작성(코드는 가드).
- **팀 작성 점검 세트 50개**: 튜터 TU-16의 100개 중 50개를 통합이 쓴다(가드 쪽 변형 포함, 레드팀 IN-08과 겸함). 튜터 A가 11/16~20 오답 분석에 집중하도록.
- **운영진 질의 묶음**: 가드 §11-1 · 튜터 §11-1 · 이 PRD §10-1을 한 번에 보내고 답을 세 PRD에 배포.
- **발표 자료 뼈대 · 팀 provenance 합본**: 튜터 PRD §9 "서버 전체 provenance · 시연 체크리스트 위치는 리더 결정"의 후보.

## 6. 예산

전제: nano `effort=low` 판정 p50 2.6s · p95 3.9s(R12 §4, 가드 #51 실측으로 교체 `[측정]`), 생성 p50 ≈4s(추정), 임베딩 0.1~0.3s.

| 경로 | nano | 임베딩 | p50 / p95 (추정) |
|---|---|---|---|
| `/tutor` 생성 경로(≈72%) | (retrieve ∥ judge_input) → 생성 → judge_output = 3 | 1 | 9s / 13s |
| `/tutor` block · abstain(≈28%) | 1 | 1 | 3s / 4s |
| `/guard` | 가드 PRD v6(2 병렬) | 0 | 3s / 4.5s |
| 채점 1회 | 튜터 ≈730 + 가드 ≈430 ≈ **1,160** | ≈300 | 600 ÷ 64 × ~6s ≈ **60~80s** |

---

## 7. 운영 절 (요약)

| ID | 항목 | 위치 |
|---|---|---|
| O1 · O2 | 오탐 · 미탐 분모 분리, 실패 4분류 | IN-06 |
| O3 · O4 · O5 | 레드팀 매트릭스 · 실패 조건 워크시트 · 공격 시드 | IN-08 |
| O6 | 로그 grep(`OPENAI_API_KEY` · `TEAM_TOKEN` · `Bearer` · 프롬프트 첫 문장 · 공개 세트 질문) | IN-09 · IN-10 |
| O7 | 단계별 지연 p95 · p99 | IN-06 |
| O8 | 토큰 다이어트(top-2, 6턴 창, 역할별 `max_completion_tokens`) | I5 · I6 |
| O9 · O10 | 교육 도메인 위협(pii 13 · emotional 9 · harmful 16 실재) ↔ 범주 표 | IN-03 · IN-08 |
| O11 | Before/After 3건(베이스라인 vs 최종) | 발표 |

---

## 8. 일정 (관문일 = 가드 master-plan §2 · 튜터 PRD §15.1과 동일)

| 단계 | 기간 | 통합 산출 | 의존 |
|---|---|---|---|
| S0 | 11/10 | 공개 세트 분석(완료), 이 PRD · CI PR, 회의(I1 · 디렉터리 · 역할) | 가드 C0 · 튜터 T0 |
| S1 | 11/11–11/15 | `contracts` · `actions.py` · 결정적 수정 · 튜터 라우터 조립 · 하네스 v1(recall 측정 포함) · **부하 1차(실제 API, 가드 · 튜터와 같은 실행)** · 11/15 제출 당번 → **M1 11/15 v1 제출** | 가드 C1(#47 · #48 · #49, 11/11~12) · C2, 튜터 T1 · T2 |
| S2 | 11/16–11/20 18:00 | ablation 프리셋 전부 · 레드팀 · 홀드아웃 ≤3회 실행 · 열린 역할(IN-12: 점검 세트 50개 · 운영진 질의 묶음 · 발표 뼈대) → **M2 동결 11/20 18:00 · 동결본 제출 당번** | 가드 C3, 튜터 T3 |
| S3 | 11/21–11/24 | provenance · 체크리스트 · 발표 자료 · 최종 회귀 → **M3 최종 제출 11/24 18:00** (검증된 커밋을 11/22~23에 미리 내고 11/24는 비상용) | 가드 C4, 튜터 T4 |
| S4 | 11/25 오전 | 제출 없음. 서버 상태 확인 · 리허설 | |

---

## 9. 리스크

| 리스크 | 대응 |
|---|---|
| 11/16~20 통합 부하가 비는 주 | 열린 역할로 채운다(§5 IN-12): 팀 작성 점검 세트 50개 · 런박스 배포 · 제출 실행 · 운영진 질의 묶음 · 발표 뼈대 |
| 가드 #47 골격이 11/12 넘어감 | 베이스라인 `server.py`에 튜터 라우터만 붙인 임시 조립으로 S1 진행 |
| `judge_output` 시그니처가 가드 v6에서 바뀜(messages 기반) | IN-01에서 가드 interface.md를 기준으로 맞춤, 튜터는 어댑터 1함수 |
| 프록시 처리량 → 채점 5분 초과 | 부하 64 · 게이트 크기는 가드 #51 실측 후 |
| 자기 판정 삭제로 abstain 과다 | 비율 측정, 임계 조정 |
| 최종 제출 사고 | 같은 커밋 4개 런박스, 순차 제출, 당번 |

---

## 10. 미결

### 10-1. 엘리스 운영진 확인
1. nano 생성 예상 질문 · 임베딩 **파일 저장 · 재사용** 허용 여부
2. 공개 세트 유사 예시 동적 삽입 범위
3. 로그 "길이"에 단계별 ms 포함 여부
4. 인용 F1 비교 단위(실제 채점기도 섹션 ID 정확 일치인지)
5. 채점기 실제 동시성(64 고정인지)

### 10-2. 회의 `[회의]`
I1 검색 소유 · 디렉터리(`integration/` · 루트 `app.py`) · 역할 · `create_app` 튜터 라우터 주입 · 라우터별 예외 기본값 · `judge_output(category=)` · `format_material` · T1 분리(I11) · **파이썬 프로젝트 구성**: 루트 `pyproject.toml`을 uv workspace(`members = ["guardrail"]`)로 둘지, `integration/` · `tutor/`를 가드 패키지 밖 별도 패키지로 둘지(가드 CI의 `guardrail/uv.lock` 캐시 경로는 그대로 유지 가능)

---

## 11. 가드 의존 (가드 이슈 → 우리가 받는 것)

| 가드 이슈 | 받는 것 | 우리 쪽 대응 |
|---|---|---|
| #47 서버 골격 | `create_app()`, 인증 · 예외→200 · 메타 로그 · lifespan | 튜터 라우터 주입 요청(IN-05) |
| #48 LLM 게이트 | `llm.complete()` · `slot()` · `probe()` | 튜터 호출이 같은 게이트 사용 |
| #49 채점 · 분할 | `eval/score.py` · `splits/v1.json` | IN-06이 import, 분할 공유(I12) |
| #50 첫 제출 | 런박스 절차 · 재기동 | IN-10 체크리스트와 합침 |
| #51 nano 실측 | 지연 p50 · p95, 파라미터 수용 | §6 예산표 교체 |
| interface.md D1-2 | `judge_input` · `judge_output` | IN-01 · IN-04. `format_material` 추가 요청 |

---

## 12. 11/10 확인값 (출처)

**SPEC**: 포트 8000 · 3xx 금지 · 401 · `answer` ≤4,000 유니코드 문자 · 약 600요청 동시 64 · 2~3분 목표 5분 끊김 · 형태소 분석기 ○ · 직접 학습 분류기 ○ · few-shot · 유사 예시 ○ · KB 파생물 ○.
**KB**: 과목당 16, 총 48섹션, 3,403~5,305자(p50 4,364), CSV `<ID>-table<n>.csv`(PY 6 · PR 9 · FIN 8), HTML 1개, 이름은 본문 자연어.
**공개 세트**: 튜터 action answer 111 · refuse 24 · abstain 20 · scaffold 16 · redirect 12 · escalate 9 · clarify 8; evidence 0 또는 1; 턴 1~11(p50 7); 버튼 문구 "학습자료의 내용을 요약해 주세요" 28 외; 가드 block 56 / allow 144, 교차표 순수, 턴 ≤5, unsupported 39개 중앙값 15자; `span_f1` = 문자 집합 F1; faithful 근거 = 우리 citations; complied = block 4범주; 판정 실패 5% 규칙; 중학생 · "~해요" 체.
**측정 대기 `[측정]`**: nano 지연(가드 #51) · 부하 64 · recall@1/2/3 (BM25 / kiwi / 임베딩 / RRF / doc2query) · 자기 판정 빈 답변 비율 · harmful 세분 · verbosity · json_schema · stop 지원.
