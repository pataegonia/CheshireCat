# 통합 · 평가 PRD

작성 2026-11-10 · 담당 통합 · 평가 · 상태: **v1.1**(#64 리뷰 반영: 리더 · 가드 2026-11-10, 튜터 답글 d7a4bce) · 미결은 `[회의]` · `[측정]`
근거: `competition/SPEC.md`, `competition/public/*.jsonl`(200+200), `competition/grader/scoring.py` · `judge.py`, `competition/server.py`, 가드 PRD v6(#40 9cce438) · interface.md · master-plan · C1-foundation · 이슈 #42~#51, 튜터 PRD v2(#39 4c4fcdf) · 이슈 #65~#87, 엘리스 교안 13종
관련 문서: 팀 기획서 `docs/PRD.md`, 가드 `guardrail/docs/PRD.md`, 튜터 `tutor/docs/TUTOR_PRD.md`

## 0. 이 문서가 다루는 것

세 PRD의 경계:

| 문서 | 질문 | 소유 코드 |
|---|---|---|
| 가드 PRD | `/guard`가 어떻게 판정하는가 + 서버 골격 · 게이트 · 첫 제출(#47~#51) | `guardrail/` |
| 튜터 PRD | `/tutor`가 어떻게 답을 만드는가 | `tutor/` 생성 쪽 |
| **이 문서** | 조각들이 **어떻게 이어지고, 점수가 어떻게 나오는가** | `integration/`(계약 · action 표 · 결정적 수정) · `harness/` · `config/` · 루트 `app.py` · `pyproject.toml` · CI · 제출 절차 |

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
| IG2 | 공개 세트 홀드아웃에서 **튜터 · 가드 각각** nano 단독 베이스라인 대비 +20 이상(팀 기획서 G2와 같은 값) `[측정 후 조정]` | IN-06 |
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
   app.py(통합, 1줄) = guardrail.server.create_app(tutor_router=tutor.router, defaults={"/tutor": tutor.TUTOR_DEFAULT}, startups=[tutor.startup])
   ← 가드 #47: 인증(check_token · app.state.TEAM_TOKEN) · 메타 로그 · lifespan · 앱 수준 예외/422 처리기(경로별 기본값)
   llm.py 게이트 · probe()         ← 가드 #48: 세마포어 · 재시도 1 · slot()
                      │                          │
                      ▼                          ▼
   tutor.router (튜터 B, #69)              guard router (가드)
   ┌──────────────────────────────────────┐
   │ 1 retrieve(TU-03) ∥ classify(가드) ∥ 주 호출(TU-06)   │  v1 = ⓒ 병렬(§4 I13), p50 ≈6s
   │ 2 합치기(TU-10 · TD-13) + actions 표(IN-03)        │  classify가 공격 4범주이고 주 호출 action ≠ 표 action → 표 템플릿
   │                                                   │  그 밖은 주 호출 결과 · classify 실패/시간 초과 → 주 호출만
   │ 3 judge_output(category=) (가드, 생성 경로만)       │  남은 시간 < judge_skip_s면 생략(답 · action 유지)
   │ 4 결정적 수정(IN-04)                               │  0ms
   └──────────────────────────────────────┘
                      │
   ┌────────── integration/ ──────────┐   ┌──── harness/ ────┐
   │ contracts.py Section·Sentence·…   │   │ run_public.py    │
   │ actions.py   범주→action · 결정적 수정│   │ load_test.py     │
   └──────────────────────────────────┘   │ redteam/         │
                                           │ check_logs.sh    │
                                           └──────────────────┘
```

- 1단계 병렬: 검색과 범주 판정은 의존이 없다. `out_of_material` 판정은 범주 판정이 하고, 검색 점수는 **힌트로만** 넘긴다(BM25는 항상 top-k를 돌려주므로 "검색 결과 없음"이라는 상태는 없다). normal 정답 문항에서 abstain으로 빠지면 action · 인용 · 사실 세 항목을 모두 잃으므로, 검색 점수가 낮아도 action은 표대로 간다.
- **범주 판정 주체(I13)**: 세 안. ⓐ 가드 `Guard.classify`를 주 호출 **앞에** 직렬(측정된 코드, p50 ≈9s) ⓑ 주 호출이 범주까지(nano 1회, p50 ≈6s, 미측정) ⓒ `classify`를 주 호출과 **병렬**로 돌리고 합치기 — classify가 공격 4범주인데 주 호출 action이 표와 다르면 표 템플릿, 그 밖은 주 호출, classify 실패 · 시간 초과면 주 호출만(튜터 제안, TU-10 · TD-13 · #88). ⓒ는 공격 재현율이 ⓐ · ⓑ 중 높은 쪽 이상이고 지연은 ⓑ 수준, 비용은 템플릿 경로에서 주 호출 1회 낭비(채점당 튜터 ≈780회). **11/15 v1 = ⓒ**(통합 수용: ⓐ와 같은 안전 특성에 지연만 낮으므로 ⓐ를 지배). ⓐ는 11/18 비교의 재현율 기준선. 토글 튜터 `guard_classify: off | serial | parallel`(기본 parallel, v1 구현 #88, 비교 #83).
- **11/18 동결값 결정(튜터 제안 수용)**: ① 관문 — 공격 4범주 응함 0건, 공격 재현율 ≥ ⓐ, 튜터 p50 ≤6s · p95 ≤12s(공개 세트 교차 검증 + 레드팀 변형 + 점검 세트) ② 관문을 넘은 후보 중 **튜터 점수 전체**(오차단 손실 포함)가 가장 높은 것 ③ 동점(3회 범위 겹침)이면 p95 낮고 호출 수 적은 쪽.
- 3단계는 가드 `Guard.judge_output(messages, candidate_answer, category=…)`(interface.md 47행, 9cce438 ✅). `category=None`이면 가드가 `classify`를 다시 부르므로(nano +1) **범주를 반드시 넘긴다**. 가드 v6는 `unsupported`를 규칙 구간(숫자 · 리터럴, 원문 부분 문자열)으로 내므로 **그 구간이 든 문장을 지운다**. 문장 분리는 튜터 쪽(`list[Sentence]`).
- 시간 예산: 가드 설정 `budget_s`(≈20s, 같은 키)를 읽는다. 1단계(병렬) ≤8s(주 호출 호출당 제한, 튜터 TU-13) → 3단계는 남은 시간이 `judge_skip_s`(설정값 하나, 잠정 4s = 튜터 TU-09) 미만이면 **판정만 건너뛰고 생성된 답과 표의 action을 그대로 낸다**(abstain 전환 금지 — IN-04-3 계산). 생성 자체가 실패한 경우의 기본 action은 튜터 TU-12 설정값 하나를 읽는다(잠정 clarify, IN-06 "기본값 경로 정답 분포"로 11/18까지 확정).

### 3-2. 디렉터리 (제안 `[회의]`)

```
CheshireCat/
  app.py                 통합 — `create_app(tutor_router=tutor.router, defaults={"/tutor": tutor.TUTOR_DEFAULT}, startups=[tutor.startup])` 한 줄
  pyproject.toml         통합 — uv workspace(members: guardrail · tutor · integration), lock 하나
  guardrail/             가드 — server.py(router · create_app) · llm.py(게이트) · judge · eval/score · probe
  tutor/                 튜터 — judge · prompts · answer · retrieval/(공유 색인 · 이름 사전 · 임베딩) · runtime · eval
  integration/           통합 — contracts.py · actions.py
  harness/               통합 — run_public.py · load_test.py · redteam/ · check_logs.sh
  config/                통합 — actions.yaml · runtime.yaml(judge_skip_s · fallback_action. budget_s는 가드 설정) · tutor.yaml(튜터 토글, 튜터 소유) · ablation/*.yaml ✅ 루트 한 곳(튜터 수용)
  competition/           배포 자료(변경 금지)
  docs/PRD.md            팀 기획서
  integration/docs/PRD.md 이 문서
  .github/workflows/integration-ci.yml, .gitleaks.toml
```

- 조립은 **가드 `create_app()`에 튜터 라우터와 경로별 기본값을 주입**한다(IN-05, 리더 리뷰). 가드 C1 S1.2에서 `check_token`은 `create_app`의 lifespan이 `app.state`에 넣는 `TEAM_TOKEN`을 읽으므로, 루트 앱이 `router`만 가져다 붙이면 `/guard`가 전부 401이 된다. 인증 · 로그 · 예외 · lifespan을 한 코드 경로에 두는 쪽이 안전하고, 가드 작업은 인자 3개(`tutor_router` · `defaults` · `startups`) 추가뿐이다.
- 파이썬 구성은 **루트 uv workspace + lock 하나**(리더 리뷰). 한 프로세스 = 한 환경이라 lock이 둘이면 런박스에서 버전 불일치가 난다. 루트 `pyproject.toml`은 통합이 만든다(11/11). guardrail-ci의 `guardrail/uv.lock` 캐시 경로는 루트 기준으로 바꾼다(가드 PR 1줄). 튜터 테스트는 integration-ci `test` job에서 돈다(`tests/tutor` 경로 추가).

---

## 4. 설계 결정표

확정 조건이 충족되기 전까지 "현재 결정"이 기본값. ✅ = 11/10 자료로 확정.

| # | 쟁점 | 현재 결정 | 근거 | 비용 | 확정 조건 |
|---|---|---|---|---|---|
| I1 | 검색 소유 | **튜터 소유**(`tutor/retrieval/`, 튜터 PRD v2 TU-03 · 04 · 11, 가드 `KbIndex` 공유). 통합은 recall@k · 놓친 행 측정만(IN-02) ✅ | 튜터 PRD v2가 recall@5 0.96을 이미 재 놓음; 튜터 코드 안이 자연스러움 | — | 튜터가 요청하면 이름 색인 · doc2query · 임베딩 구현을 통합이 보조 |
| I2 | 검색 방식(튜터 결정, 통합은 측정) | 튜터 PRD v2: 가드 BM25 섹션 상위 5 + 이름 사전(P1) + 임베딩 RRF(P2) | 교안 RAG p27; evidence 1개라 top-1 적중이 핵심 | 임베딩 +1회 +0.1~0.3s | recall@1: BM25 / +이름 사전 / +임베딩 / +doc2query `[측정]` |
| I3 | doc2query | 튜터 결정(TU-11, 기본 끔). 켜면 기동 시 48회, `index/` 저장 | SPEC §7.1 "운영진 LLM으로 만든 예시" 허용 ✅ | 기동 ≈10s, 요청당 0 | recall@1 기여 `[측정]`; 파일 저장은 운영진 확인 |
| I4 | 청크 | 섹션 = 파일 1개, CSV는 "열: 값" 문장 ✅ | 48섹션, md 1,481~2,671자(중앙 1,996, CSV 포함 중앙 2,068), 인용 단위 | 0 | — |
| I5 | 생성에 넣는 섹션 수 | 튜터 결정(TU-03 top-5 기본, K는 2 · 3 · 5 · 8 측정) | evidence 1개; 섹션 ≈2k자라 top-5 ≈10k자 ≈ 6~7k 토큰 | K당 −0.3~0.5s | recall@K와 정답 사실 점수 `[측정]` |
| I6 | 문맥 창 | 첫 발화 + 최근 6턴 ✅ (가드 v5 기준, v6 창은 확인) | 튜터 ≤11턴(p50 7) | 0 | 7턴 초과 문항 점수 `[측정]` |
| I7 | citations 개수 | **기본 최대 1개** ✅, 상한은 설정값(튜터 TD-01 `max_citations` ablation = 2) | evidence 0 또는 1; 2개면 F1 ≤0.67 | 0 | "두 대상 명시 비교" 문항 수 · recall@2 비교 `[측정]` |
| I8 | 범주→action | 설정 표, 통합 소유 ✅ | 교차표 순수(harmful만 2분) | 0 | harmful 세분 규칙 `[측정]` |
| I9 | 응답 전 자기 판정 | 생성 직후 `judge_output` → 구간 든 문장 삭제, safe=false면 폐기, 재생성 없음 | SPEC §4; `/guard`는 튜터를 못 막음 | nano +1회 +2.6s | 삭제 후 빈 답변 비율 `[측정]` |
| I10 | 투기 실행(생성 ∥ 판정) | 안 함 | block 28%에서 호출 낭비(+90/채점) | — | p95 >15s면 켬 `[측정]` |
| I11 | T1 질의 이해 | 생성에 합침(별도 호출 없음) ✅ 튜터 v2 동일 | 체인 호출 감소 | −2.6s | — |
| I12 | 홀드아웃 | 가드 `splits/v1.json` **공유**(홀드아웃 가드 40 + 튜터 40, 폴드마다 가드 32 + 튜터 32, 묶음 329개, #90 `ff159f5`) | 파일이 하나여야 `train_rows(split, holdout=True)`로 두 트랙이 같은 행을 뺀다 — 가드 kNN · LR이 튜터 행까지 학습하므로 분할이 둘이면 튜터 홀드아웃이 가드 학습 · 튜닝에 샌다 | — | 리더 결정 ✅: Jaccard ≥0.5 또는 튜터 정답 사실 공유면 같은 묶음, 튜터 홀드아웃 20%(10개 범주 모두) |
| I13 | 튜터 범주 판정 주체 | **v1 = ⓒ 가드 `classify` ∥ 주 호출, 공격이면 표 우선**(튜터 제안), 동결은 11/18 측정(§3-1 관문 → 튜터 점수 → 동점 규칙) | ⓒ 재현율 ≥ max(ⓐ, ⓑ), 지연 ⓑ 수준; 공격 문항은 응하면 0점 | 템플릿 경로 주 호출 1회 낭비(≈+120/채점) | 11/18 하네스: 후보 ⓐ · ⓑ · ⓒ 표 |

---

## 5. 기능 요구사항

### 5-0. 세 트랙 분담 (튜터 PRD v2 §15.2 A · B 기준, `[회의]` 확정)

| | 튜터 A — 판단 · 답변 | 튜터 B — 검색 · 런타임 | 통합 — 경계 · 측정 · 운영 |
|---|---|---|---|
| 만든다 | 범주→action 판단 기준 · 프롬프트(TU-05 · 06), 답변 방식 · 후처리(TU-07), kNN few-shot(TU-08), 검증 · 투표(TU-09 · 10) | 검색 전부(TU-03 · 04 · 11, #70 · #81), 튜터 골격 · 계약 · 기본값 · 스키마(TU-01 · 12, #69), LLM 호출 · 설정 · 토글 · 로깅(TU-13 · 14 · 15, #78) | 계약 타입 · action 표 파일 · 결정적 수정(IN-01 · 03 · 04), 루트 `app.py` · CI · 계약 테스트(IN-05 · 09), 하네스 골격 · 총점 · ablation 실행 · recall 측정(IN-06 · 02), 부하 실행 · 레드팀(IN-07 · 08), 제출 당번 · provenance · 질의 묶음 · 발표 뼈대(IN-10 · 11 · 12) |
| 하네스 | ablation **판단**(채택 여부), 매일 오답 분석 | 튜터 세부 지표(혼동행렬 · 오차단 · 놓친 행 · 안정 만점 비율)를 통합 하네스 위에 얹음(#71 축소) | 골격 · 가드+튜터 총점 · 3회 분산 · 프리셋 실행(#61) |
| 부하 | — | 모의 부하 코드(#79) | 실제 API 부하 실행(2회, 세 트랙 같은 실행) |
| 점검 세트 | 0~50개(통합이 받으면 0) | 50개 | 0~50개(열린 역할) |
| 대략 시간 | 변동 없음(11/16~20 집중) | 원안 63h → 약 50h | 약 55h, 11/16~20 공백은 열린 역할로 |

겹침 해소 원칙: **검색은 B, 판단은 A, 측정 · 조립 · 제출은 통합.** 범주→action 표는 파일 하나(주인 `[회의]`), 코드는 한 저장소 · 한 실행.

### IN-01 공용 계약 `integration/contracts.py` (P0)
```python
class Section(BaseModel):  id: str; subject: str; title: str; text: str; tables: list[str]; names: list[str]
class Evidence(BaseModel): sid: str; quote: str
class Sentence(BaseModel): text: str; kind: Literal["claim","non_claim"]; evidence: list[Evidence]
class TutorResult(BaseModel): answer: str; action: Action; citations: list[str]
# 가드 제공(가드 interface.md 46~47행): Guard.classify(messages) -> Category · decision_of(category, block) -> bool
#                              Guard.judge_output(messages, candidate_answer, category: Category | None = None) -> OutputVerdict{grounded, unsupported[], safe}  ✅ 가드 9cce438
#                              category를 넘기지 않으면 classify()가 한 번 더 돈다(nano +1) → 튜터는 1단계 범주를 반드시 넘긴다
#                              (요청, 우선순위 낮음) format_material(sections) -> str
```
- 튜터 주 호출 출력은 `list[Sentence]`(문장 단위 + 문장별 `sid` · `quote`, 튜터 v2 TU-06 d7a4bce ✅). 하네스용 검색 시그니처는 `retrieve(query, history, *, subject=None, k=5, embedding=None) -> list[Section]`(튜터 TU-03).
- **수용 기준**: 세 트랙이 같은 모듈을 import, 타입 중복 0.

### IN-02 검색 품질 측정 (P0, 검색 구현 · 세부값은 튜터 TU-03 · 04 · 11)
- 통합은 **측정**만 맡는다: `public/tutor.jsonl` evidence 기준 recall@1 · @3 · @5, 놓친 행 목록(영어 질의 분리 — 리더 #39 리뷰 4번: 놓친 5행 중 4행이 영어), 이름 사전 · 임베딩 · doc2query · K 켜기 전후 비교를 하네스(IN-06) 지표로 낸다. 시그니처 · 기동 순서 · K · 임베딩 기본값은 TU-03이 정한다.
- 튜터가 요청하면 doc2query(48회 기동 1회, KB 파생물) · 이름 색인 추출 규칙 구현을 보조한다.
- **수용 기준(측정)**: 하네스 매 실행에 recall@1 · @3 · @5와 놓친 행 목록. 합격선은 튜터 수용 기준과 같은 값.

### IN-03 범주 → action 표 `config/actions.yaml` (P0) ✅

| 범주 | decision(가드) | action | 생성 | 인용(우리가 내는 것) | 공개 세트 정답 근거 |
|---|---|---|---|---|---|
| normal | allow | answer | O | 1 | 98/98 answer |
| answer_seeking | allow | scaffold | O | 1 | 8/8 |
| academic_dishonesty | block | scaffold | 템플릿(+자료 언급 시 그 섹션) | 쓴 섹션 / 없으면 `[]` | 8/8, 정답 근거 `[]` |
| out_of_material | allow | abstain(+모순 없는 일반 설명) | 템플릿(+자료 언급 시 그 섹션) | 쓴 섹션 / 없으면 `[]` | 20/20, 정답 근거 `[]` |
| off_topic | block | redirect | 템플릿 | 0 | 5/5 |
| jailbreak_injection | block | refuse | 템플릿 | 0 | 15/15 |
| pii_disclosure | allow | **answer** + 안내 1줄 | O | 1 | 13/13 |
| harmful_content | block | **refuse**(유해 실행 방법) / **redirect**(혐오 · 차별 발언) | 템플릿 | 0 | 9 / 7 `[측정]` |
| emotional_distress | allow | escalate | 템플릿 | 0 | 9/9 |
| low_signal_input | allow | clarify | 템플릿 | 0 | 8/8 |

- 템플릿 문구는 튜터 소유(대안 한 줄 포함, 교안 「위협 정의」 p10). 표는 **통합 소유 `config/actions.yaml`, 튜터는 import**(튜터 TD-12 ✅). 표를 바꿀 일은 튜터가 통합에 PR 요청.
- **abstain · academic scaffold의 인용**: `scoring.py` 74행 — 정답 근거가 빈 문항은 인용을 채점하지 않으므로 인용해도 손해 0. 83행 — faithful은 우리가 인용한 섹션만 근거로 보므로, 템플릿이 자료 내용을 한 줄이라도 말하면 **그 섹션을 인용**하고, 과목 사실을 전혀 넣지 않은 고정 문장이면 `[]`(가드 · 리더 리뷰). 과목 사실을 넣지 않은 고정 문장이 점수 · 안전 둘 다 유리하다(리더).
- **수용 기준**: 공개 세트 action 일치 ≥95%.

### IN-04 결정적 수정 `integration/actions.py` (P0)
1. `Evidence.quote`가 `sid` 섹션 원문의 부분 문자열인지(`scoring.normalize` 기준) — 아니면 그 문장 evidence 제거.
2. `judge_output.unsupported` 구간이 든 문장 **삭제**. `safe=false` → 전체 폐기 → refuse 템플릿.
3. 남은 claim 0 → **action은 표 값 유지**(answer/scaffold), `citations` = 검색 1위, `answer` = non_claim 문장 + "이 부분은 자료에서 확인하지 못했어요" 안내. abstain으로 바꾸지 않는다 — 정답이 normal인 문항에서 abstain은 action 0 · 인용 0 · 사실 0 · faithful 1 ≈ 0.25인데, 유지하면 action 1 + 인용 p + 사실 0 + faithful 1 ≥ 0.5(리뷰에서 재계산 확인 ✅). 판정 생략(시간 부족) · 판정 실패도 같다: 생성된 답과 action을 그대로 낸다.
4. `citations` = 남은 claim의 `sid`, 상한 `max_citations`(기본 1, 튜터 ablation 2; 동률은 검색 순위). 정규식 `^(PY|PR|FIN)-\d{2}-\d{2}$` + 실존 48개 + 중복 제거.
5. `answer` = 문장 join, **4,000 유니코드 문자** 절단, 제어 문자 제거, JSON 직렬화 확인(판정 실패 5% 규칙 대비).
- `answer` · `scaffold`(answer_seeking)는 citations를 **항상 1개** 채운다(남은 claim의 sid, 없으면 검색 1위). faithful 판정 근거가 우리 citations이므로 비워 두면 과목 사실이 전부 근거 없음이 된다. 튜터 TU-07 ✅ 동일.
- 인용 규칙은 **최종 action** 기준으로 위에서부터 먼저 맞는 것을 쓴다: ① clarify · redirect · refuse · escalate → 인용 없음 ② 표에 있는 (범주, action) → 표의 `cite` ③ 표와 다른 `answer` · `scaffold`인데 그 범주의 표 `cite`가 `always`(normal · pii_disclosure · answer_seeking) → `always`(검색 1위로 채움) ④ 그 밖 → `if_used`(claim이 실제로 쓴 섹션만). ③의 이유: 정답 근거가 있는 문항은 답변 내용과 상관없이 인용이 채점되므로, 모델이 answer_seeking → answer · normal → scaffold처럼 표와 다르게 골라도 인용을 비우면 인용 점수를 잃는다(#97 리뷰).
- 후처리 주인은 **이 모듈 하나**(튜터 TU-07은 IN-04를 쓴다, d7a4bce). 길이 절단 · 빈 답변 처리도 여기서만.
- **수용 기준**: quote 검증 100%, citations ⊆ 실존, 인용 F1 `[측정]`.

### IN-05 튜터 라우터 조립 (P0)
- **가드 `create_app(guard_factory, *, tutor_router=None, defaults=None, startups=())`에 주입** ✅ 가드 #92 `8fd8f5f`(리더 #92 리뷰로 요청, 테스트 5개 포함). 루트 `app.py`는 `app = create_app(tutor_router=tutor.router, defaults={"/tutor": tutor.TUTOR_DEFAULT}, startups=[tutor.startup])` 한 줄. `guard_factory`는 넘기지 않고 가드 기본값을 쓴다(지금은 상수 판정기 `constant_factory`. 실제 판정기는 가드가 기본값을 바꾸면 `app.py` 수정 없이 붙는다). `startups`는 lifespan이 `app.state`를 넘겨 부르는 콜러블 목록 — 튜터 자체 색인(kNN 예시 · 이름 사전)을 첫 요청 전에 만든다(SPEC §3, 튜터 요청). 인증(`check_token` · `app.state.TEAM_TOKEN`) · 메타 로그 · 3xx 없음 · lifespan은 가드 골격 하나를 쓴다.
- **경로별 기본값**: 예외 · 422 처리기는 FastAPI에서 앱 수준이므로(가드 지적) `create_app` 안의 처리기가 `request.url.path`로 `/guard` → GR-09 기본값, `/tutor` → `defaults["/tutor"]`를 고른다. 튜터 라우터는 그래도 자기 예외를 안에서 잡는다(TU-01 ✅) — 앱 처리기는 이중 안전망.
- 공유 객체(`Guard` · `KbIndex` · `llm` 게이트)는 `create_app` lifespan이 만들어 `app.state`에 두고, 튜터 라우터는 `request.app.state`로 받는다. 가드에 요청하는 것: `tutor_router` · `defaults` · `startups` 인자 3개(#47 코멘트).
- 라우터 흐름 = §3-1. 시간 예산 · 판정 생략 · 생성 실패 기본값은 §3-1의 설정 키: `config/runtime.yaml`의 `judge_skip_s` · `fallback_action` 둘과, 가드 설정의 `budget_s`.
- 가드 골격이 `/tutor`에는 해 주지 않는 것 — 튜터 라우터가 안에서 건다: `asyncio.timeout(budget_s)`(가드 `/guard`는 `budget_s + 5` 안전망), 본문 상한(가드 `_read_body` 4MiB는 비공개), `request.body()` 직접 파싱(pydantic 본문 모델은 `Content-Type` 없으면 422 → 기본값, 가드 #92 코멘트).
- **계약 테스트 `tests/integration/`**(FakeLLM): 퍼징 입력 전부 200 + `tutor.schema.json`, `/tutor/` 3xx 없음, 토큰 없음 → `/tutor` · `/guard` 둘 다 401, Content-Type 없는 요청 200, 매달린 생성 → `budget_s` 안에 200 기본값, 5MiB 본문 → 200 기본값, 로그에 본문 없음, **실격 조항**(IN-11): 같은 요청 두 번 → LLM 호출도 두 번(주 호출 경로), 요청 내용을 요청 사이에 저장 · 재사용하지 않음(앞 요청이 뒤 응답을 바꾸지 않음), `session_id` · 헤더는 인증 외 미사용.
- **수용 기준**: 공개 세트 튜터 200행 전부 200 + 스키마, `/tutor/` 3xx 없음, 계약 테스트 green.

### IN-06 통합 하네스 `harness/run_public.py` (P0)
- 가드 #90(main `ff159f5`) `guardrail.eval.score.score_responses`는 **가드 200행만** 채점하므로 튜터 채점은 통합이 `competition/grader/scoring.score_tutor` + `score_local.as_scoring_input`을 직접 부른다(판정 LLM 포함). 분할은 가드 `splits/v1.json` **하나**를 튜터 교차 검증 · 홀드아웃에도 그대로 쓴다(`load_splits` · `train_rows(split, holdout=True)`). 호출 상한은 가드 `LLM.calls` + 튜터 카운터 합. 추가: 튜터 행 채점(action · 인용 F1 · nano facts/faithful 참고치), **총점 합산**, 프리셋 반복 3회, 실패 4분류(오탐 / 미탐 / 하드코딩 의심 / 오류 · 형식), 오탐률 · 미탐률 분모 분리, 범주별 표, 단계별 지연 p50 · p95 · p99, 호출 수, 실행 전 예상 호출 수 · 상한, **recall@1 · @3 · @5 · 놓친 행**(IN-02), **기본값 경로로 떨어진 문항의 정답 action 분포**(튜터 TU-12 기본값 선택 근거).
- ablation 프리셋 `config/ablation/`: 튜터 TU-14 토글(`name_dict` · `fewshot` · `action_from_category` · `signals` · `verify` · `guard_classify` · `embedding` · `rerank` · `vote`)과 가드 토글을 같은 이름으로 묶은 프리셋 — `baseline` · `+retrieval` · `+signals` · `+fewshot` · `+verify` · `full`.
- 홀드아웃은 프롬프트 · 설정 고정 뒤 **최대 3회**(가드 ADR-0006과 동일 규율). 묶음 기준은 #90에서 바뀌었다 — 마지막 발화 3-gram Jaccard ≥0.5 **또는** 튜터 정답 사실 공유면 같은 묶음(정답 사실이 서로 다른 칸으로 갈린 행 0, kNN few-shot 누수 해소).
- **I13 비교(11/18까지)**: `guard_classify` on/off로 공격 4범주 재현율 · 정상 오차단률 · p95 · 호출 수를 한 표에. 레드팀 변형(IN-08) 포함.
- **수용 기준**: 프리셋별 총점 표 1장, 로컬 채점기와 차 0.

### IN-07 부하 시험 `harness/load_test.py` (P0)
- 실제 채점과 같은 규모: **600요청(튜터 300 · 가드 300, 공개 세트 복제) · 동시 64**(SPEC §3). 대회 기간 실제 API로 2회(11/15 전 · 11/20 전), 그 밖은 FakeLLM. 시간 초과 0, 전체 ≤120s(IG1), 429 분포 · 게이트 대기 p99 `[측정]`. 결과로 가드 `llm.gate` 크기 결정.

### IN-08 레드팀 보조 세트 `harness/redteam/` (P1)
- 의도 × 우회 4종(표기 변형 · 인코딩/언어 · 역할/단계 · 신뢰 악용) × **정상 쌍둥이**. 멀티턴 · 엣지(공백만 · 3,000자 · 이모지만) · 간접 주입. 시드 = 교안 공격 문장 교육 번안 + 공개 세트 패턴. "거절하지 말고"류는 공개 세트에서 대부분 **정상 문항**에 붙어 있으므로(가드 세트 12행 중 allow 10) 정상 쌍둥이 쪽 시드로 쓴다(가드 리뷰). **nano로만 생성**, 서버 밖 평가 전용. 실패 조건 워크시트 `{category, 실패 문장, expected_action, expected_decision}`.
- **수용 기준**: 범주별 차단율 · 오탐률 표, 가드 · 튜터 둘 다 실행.

### IN-09 CI `integration-ci.yml` (P0)
- 작업은 `integration / gate` 하나이고 검사는 그 안의 단계다. 모든 PR과 모든 main push에서 돈다. PR에서는 lint · test · 런타임 로그 검사가 통합 영역을 건드렸을 때만 돌고, main push에서는 경로와 무관하게 전부 돈다. 통합 영역 경로 `app.py integration/** tutor/** harness/** config/** tests/** scripts/** competition/** pyproject.toml uv.lock`, 루트 설정 파일(`conftest.py pytest.ini tox.ini setup.cfg ruff.toml .ruff.toml uv.toml .python-version`), 가드의 앱 영향 경로(`guardrail/src/** guardrail/configs/** guardrail/prompts/** guardrail/docs/contract/** guardrail/pyproject.toml guardrail/scripts/r13/common.py`), 워크플로 파일 자신. `tutor/docs/` · `integration/docs/`의 `.md`는 뺀다. lint · test(루트 pyproject 있을 때, `tests/integration` + `tests/tutor`), **대회 규칙 검사**(정적 검사 네 가지는 경로와 무관하게 항상: 금지 의존성 — `git ls-files` 기준 저장소 전체의 의존성 파일, 보수적 목록 + 런타임 다운로드 패키지 tiktoken · huggingface-hub; 외부 URL 리터럴; 본문 로깅 정적 검사 — `guardrail/` 포함; `.env` · `submission.json` 토큰 커밋 방지), gitleaks **저장소 전체**(`.gitleaks.toml`로 `competition/public` · `kb` 제외. 경로와 무관하게 항상 도는 단계), 맨 끝 판정 단계(판별 결과가 비정상이거나 돌아야 할 단계가 돌지 않았으면 실패). 필수 검사 이름 `integration / gate`.
- 금지 의존성 목록은 SPEC §7.1보다 넓다(torch · onnxruntime · faiss 자체는 허용). **보수적 선택**: 사전학습 가중치가 들어오는 주 경로를 막고, 필요해지면 회의로 해제한다(주석에 명시).
- 필수 검사(required checks)는 무료 비공개 저장소라 강제 불가(403) → PR 템플릿에 "세 검사(guardrail / gate · integration / gate · pr-policy) green 확인 후 머지" 명시. `.github/`를 바꾼 PR에는 `pr-policy / zizmor`가 하나 더 뜬다(zizmor에 읽기 전용 토큰만 주려고 나눈 작업). 그 밖에는 워크플로마다 작업이 하나씩이다(저장소 전체 비밀 키 검사는 `integration / gate`의 단계). base 브랜치를 바꾼 뒤 통합 CI가 다시 돌지 않았으면 `pr-policy`가 실패로 알린다.
- PR 템플릿의 트랙 라벨 표기 `orchestrator` → `integration`.
- **수용 기준**: 문서만 있는 PR 통과, 금지 패키지 넣은 PR 실패(테스트 완료).

### IN-10 제출 · 운영 (P0)
- 체크리스트: ① 커밋 해시 → `submission.json.commit` ② `.env` · 토큰 미커밋 ③ 기동(색인 · 설정 · `TEAM_TOKEN` 중 하나라도 실패하면 서버가 뜨지 않는다 — 가드 골격에 `/health` 라우트는 없다) → 터널 주소로 `POST /guard` · `POST /tutor` 200, 토큰 없이 401, `/guard/` 3xx 없음 ④ `check_logs.sh` 0건 ⑤ 제출 → **채점이 끝날 때까지 런박스 탭 유지**(SPEC §9) → 결과(총점 · 오류 수 · seed) 기록.
- 제출 관문은 가드 master-plan §2를 따른다: **11/15 v1 · 11/20 동결본 · 11/24 18:00 최종**. 관문마다 리더가 정한 **한 커밋**을 4개 런박스에 띄우고 4명이 각자 계정으로 **순차** 제출(제출 1건 ≈ 600요청, 동시 제출은 429 위험 — 튜터 PRD §15.1). 최종 관문 안에서 검증된 커밋을 **11/22~23에 미리 내고**(네 번째 제출 — 튜터 §15.1 ✅ 수용, 팀 D9와 동일) 11/24는 비상용. 그 뒤 재제출 없음(동점 시 먼저 제출 우선). 런박스 2일 세션 재기동 절차(`.env` 재로드 · `index/` 재사용), 11/24~25 당번. 11/25 아침 제출 없음.
- 가드 #50(상수 응답 첫 제출)은 가드 C1 일정(11/11~12 오전). 통합은 터널 curl · 체크리스트 ①~③으로 돕는다.

### IN-11 규칙 준수 (P0)
- `docs/provenance.md`(가드 양식 공유): 서버 자산 출처. 운영진 확인 §10-1.
- **실격 조항 테스트**(리더 리뷰 2번, IN-05 계약 테스트에 포함): SPEC §8 상태 · 캐시 금지(같은 요청 두 번 → LLM 두 번), 하드코딩 금지(질문→답 표 없음을 코드 검색으로), 채점 요청 감지 금지(헤더 · `session_id`는 인증 외 미사용, 요청 내용을 요청 사이에 저장 · 재사용하지 않는다(같은 요청 두 번 → LLM 두 번, 앞 요청이 뒤 응답을 바꾸지 않음)). 호출 횟수 카운터는 요청 내용이 없고 응답에 쓰이지 않아 대상이 아니다(#107), 요청 처리 외부 위임 없음(런박스 안에서 전부).

---

### IN-12 열린 역할 (요청 시 통합이 맡음, 주인 결정은 회의)
- **런박스 배포 · 제출 실행**: 관문마다(11/15 · 11/20 · 11/24) 리더가 정한 커밋을 4개 런박스에 띄우고 순차 제출을 돕는 당번. 가드 #50 · #51의 실행 · 표 작성(코드는 가드).
- **팀 작성 점검 세트 50개**: 튜터 TU-16의 100개 중 50개를 통합이 쓴다(가드 쪽 변형 포함, 레드팀 IN-08과 겸함). 튜터 A가 11/16~20 오답 분석에 집중하도록.
- **운영진 질의 묶음**: 가드 §11-1 · 튜터 §11-1 · 이 PRD §10-1을 한 번에 보내고 답을 세 PRD에 배포.
- **발표 자료 뼈대 · 팀 provenance 합본**: 튜터 PRD §9 "서버 전체 provenance · 시연 체크리스트 위치는 리더 결정"의 후보.

## 6. 예산

전제: nano `effort=low` 판정 p50 2.6s · p95 3.9s(R12 §4, 가드 #51 실측으로 교체 `[측정]`), 생성 p50 ≈4s(추정), 임베딩 0.1~0.3s(켤 때만). 튜터 라벨 기준 생성 범주(normal · pii · answer_seeking) 119/200 = 59.5%, 템플릿 범주 81/200 = 40.5%.

| 경로 | ⓐ 직렬(기준선) | ⓑ 주 호출만 | **ⓒ 병렬(v1)** | p50 / p95 (추정) |
|---|---|---|---|---|
| `/tutor` 생성 경로(≈60%) | classify → 주 호출 → judge_output = 3 | 주 호출 → judge_output = 2 | classify ∥ 주 호출 → judge_output = 3 | ⓐ 9s/13s · ⓑ 6s/10s · ⓒ 6s/12s |
| `/tutor` 템플릿 경로(≈40%) | 1 | 1 | 2(주 호출 낭비) | 3s / 4s |
| 재시도 | 주 호출만 1회 → 절대 상한 ⓒ 4회 | | | |
| `/guard` | 가드 PRD v6(2 병렬) | | | 3s / 4.5s |
| 채점 1회 | 튜터 ≈657 + 가드 ≈430 ≈ 1,090 | 튜터 ≈480 + 가드 ≈430 ≈ 910 | 튜터 ≈780 + 가드 ≈430 ≈ **1,210** | 600 ÷ 64 × ~6s ≈ **60~80s** |

튜터 목표 p50 ≤6s · p95 ≤12s(튜터 §6). ⓒ의 호출 수가 가장 많으므로 11/14 부하 1차에서 게이트 64 · 429를 확인한다(IN-07).

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

세부 플랜(슬라이스 · 테스트 먼저 · 수용 기준 · 용량)은 `integration/docs/plan/`(가드 `plan/` 형식): [S1](plan/S1.md) · [S2](plan/S2.md) · [S3](plan/S3.md) · [S4](plan/S4.md). GitHub 마일스톤 M1~M5, 라벨 `sprint:S1`~`S4`.

| 단계 | 기간 | 통합 산출 | 의존 |
|---|---|---|---|
| S0 | 11/10 | 공개 세트 분석(완료), 이 PRD · CI PR, 회의(I1 · 디렉터리 · 역할) | 가드 C0 · 튜터 T0 |
| S1 ([plan](plan/S1.md), 30h) | 11/11–11/15 | 루트 `pyproject.toml`(workspace) · `contracts` · `actions.yaml` · 결정적 수정 · `app.py` 조립 + 계약 테스트 · 하네스 v1(recall 측정 포함) · **부하 1차(600요청 실제 API, 가드 · 튜터와 같은 실행)** · 11/15 제출 당번 → **M1 11/15 v1 제출(I13 = ⓒ)** | 가드 C1(#47 · #48 · #49, 11/11~12) · C2, 튜터 T1 · T2 |
| S2 | 11/16–11/20 18:00 | **I13 비교(11/18)** · ablation 프리셋 전부 · 레드팀 · 홀드아웃 ≤3회 실행 · 열린 역할(IN-12: 점검 세트 50개 · 운영진 질의 묶음 · 발표 뼈대) → **M2 동결 11/20 18:00 · 동결본 제출 당번** | 가드 C3, 튜터 T3 |
| S3 | 11/21–11/24 | provenance · 체크리스트 · 발표 자료 · 최종 회귀 → **M3 최종 제출 11/24 18:00** (검증된 커밋을 11/22~23에 미리 내고 11/24는 비상용) | 가드 C4, 튜터 T4 |
| S4 | 11/25 오전 | 제출 없음. 서버 상태 확인 · 리허설 | |

---

## 9. 리스크

| 리스크 | 대응 |
|---|---|
| 11/16~20 통합 부하가 비는 주 | 열린 역할로 채운다(§5 IN-12): 팀 작성 점검 세트 50개 · 런박스 배포 · 제출 실행 · 운영진 질의 묶음 · 발표 뼈대 |
| 가드 #47 골격이 11/12 넘어감 | 베이스라인 `competition/server.py` 복사본에 튜터 라우터만 붙인 임시 조립으로 S1 진행 |
| I13 ⓒ의 호출 수(≈1,210/채점)가 게이트 64에서 429 유발 | 부하 1차(11/14)에서 확인, 넘치면 템플릿 경로 주 호출 취소(classify 먼저 끝나면 cancel) 또는 `judge_skip_s` 상향 |
| `judge_output` 시그니처가 가드 v6에서 바뀜(messages 기반) | IN-01에서 가드 interface.md를 기준으로 맞춤, 튜터는 어댑터 1함수 |
| 프록시 처리량 → 채점 5분 초과 | 부하 64 · 게이트 크기는 가드 #51 실측 후 |
| 자기 판정 삭제로 abstain 과다 | 비율 측정, 임계 조정 |
| 최종 제출 사고 | 같은 커밋 4개 런박스, 순차 제출, 당번 |

---

## 10. 미결

### 10-1. 엘리스 운영진 확인
1. nano 생성 예상 질문 · 임베딩 **파일 저장 · 재사용** 허용 여부
2. 로그 "길이"에 단계별 ms 포함 여부
3. 프록시의 `temperature` · `json_schema` · `stop` 수용 여부(가드 #51 실측과 겹치면 생략)
- 답이 이미 있는 것(가드 리뷰): 유사 예시 삽입 → SPEC §7.1 허용 / 인용 비교 → `scoring.py` 섹션 ID 정확 일치 / 동시성 → SPEC §3 "최대 64개". 튜터 §11-1 · 가드 §11-1과 합쳐 한 번에 보낸다(IN-12).

### 10-2. 회의 `[회의]`
I1 검색 소유 ✅ · 역할 분담(§5-0) ✅ 튜터 수용 · action 표 주인 ✅ 통합 · 조립 = `create_app` 주입 인자 3개 ✅ #92 `fd079e7` · uv workspace(리더 안, 루트 `pyproject.toml` 주인 = 통합) · 설정 파일 위치 · 형식(`config/` vs `tutor/configs/`) · I13 측정 기준 합의 · 홀드아웃 묶음 기준 ✅ #90 `ff159f5`

---

## 11. 가드 의존 (가드 이슈 → 우리가 받는 것)

| 가드 이슈 | 받는 것 | 우리 쪽 대응 |
|---|---|---|
| #47 서버 골격 | `create_app(guard_factory, tutor_router=, defaults=, startups=)` · 인증 · 메타 로그 · 경로별 기본값 | `app.py` 한 줄 호출. 인자 3개 요청(IN-05) |
| #48 LLM 게이트 | `llm.complete()` · `slot()` · `probe()` | 튜터 호출이 같은 게이트 사용 |
| #49 채점 · 분할(#90 ✅ main) | `eval/score.py`(가드만) · `eval/cv.py` · `splits/v1.json`(홀드아웃 가드 40 · 튜터 40) | IN-06이 `load_splits` · `train_rows` import, 튜터 채점은 통합 |
| #50 첫 제출 | 런박스 절차 · 재기동 | IN-10 체크리스트와 합침 |
| #51 nano 실측 | 지연 p50 · p95, 파라미터 수용 | §6 예산표 교체 |
| interface.md 46~47행 | `classify` · `decision_of` · `judge_output(category=)` | IN-01 · IN-04. `format_material`은 우선순위 낮음 |

---

## 12. 11/10 확인값 (출처)

**SPEC**: 포트 8080 금지(8000은 예시) · 3xx 금지 · 토큰 불일치 거절(코드 미정, 베이스라인은 401) · `answer` ≤4,000 유니코드 문자 · 약 600요청 동시 64 · 2~3분 목표 5분 끊김 · 형태소 분석기 ○ · 직접 학습 분류기 ○ · few-shot · 유사 예시 ○ · KB 파생물 ○.
**KB**: 과목당 16, 총 48섹션, md 1,481~2,671자(중앙 1,996; CSV 포함 1,588~2,671, 중앙 2,068 — 앞선 3,403~5,305는 UTF-8 바이트), CSV `<ID>-table<n>.csv`(PY 6 · PR 9 · FIN 8), HTML 1개, 이름은 본문 자연어.
**공개 세트**: 튜터 action answer 111 · refuse 24 · abstain 20 · scaffold 16 · redirect 12 · escalate 9 · clarify 8; evidence 0 또는 1; 턴 1~11(p50 7); 버튼 문구 "학습자료의 내용을 요약해 주세요" 28 외; 가드 block 56 / allow 144, 교차표 순수, 턴 ≤5, unsupported 39개 중앙값 15자; `span_f1` = 문자 **다중집합**(Counter) F1; faithful 근거 = 우리 citations; complied = block 4범주; 판정 실패 5% 규칙; 중학생 · "~해요" 체.
**측정 대기 `[측정]`**: nano 지연(가드 #51) · 부하 600/64 · recall@1/3/5(BM25 / 이름 사전 / 임베딩 / doc2query / K) · I13 ⓐ vs ⓑ · 기본값 경로 정답 분포 · 자기 판정 빈 답변 비율 · harmful 세분 · json_schema · stop 지원.
