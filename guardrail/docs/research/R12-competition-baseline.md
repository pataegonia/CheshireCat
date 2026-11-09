# R12. 대회 기준선 조사

작성 2026-11-09 · 담당 danielcho02 · 대상: [PRD v5](../PRD.md)의 리서치 항목(본문에 반영됨)
방법: `ecc:research-ops`로 세 갈래를 동시에 돌렸다. ① `ecc:search-first`(라이브러리·구현), ② `ecc:deep-research`(설계 근거, 출처 약 20개), ③ 로컬 자산 점검(읽기 전용). 웹 검색은 Exa·WebFetch로 했다(Firecrawl 키 없음). 모든 출처는 2026-11-09 접속.

표기: **[F]** 출처가 있는 사실 · **[I]** 추론 · **[R]** 권고 · **[측정]** 공개 세트·대회 엔드포인트에서 재야 함.
웹 조사는 **설계 지식으로만** 쓴다. 여기서 찾은 데이터·목록·예시는 서버에 넣지 않는다(OT §10).

> **council·OT 대조로 바뀐 것 (2026-11-09).** 아래 권고 중 다음은 council(ADR-0009–0011)과 santa 검증에서 바뀌었다. 현재 기준은 PRD v5와 ADR이다.
> - 입력 판정 k=3 투표 → **k=1**(요청당 최대 2호출, 재시도 제외). "2표, 엇갈리면 1표 추가"는 실험 플래그.
> - 요청 마감 약 27초 → **25초**, 호출마다 재시도 1회.
> - 구간: LLM이 부분 문자열 복사 → **문장 번호 선택이 기본**. 부분 문자열 정렬(§6-4)은 정답 구간이 문장보다 짧을 때만.
> - 결정적 신호: **힌트가 기본**. 정의상 확실한 것만 직접 결정하되 공개 세트 라벨로 확인한 뒤 켠다.
> - 입력 판정 문맥: **첫 학습자 발화 + 최근 6턴**. 검색 질의에도 같은 창의 학습자 발화를 넣는다.
> - OT 대조: 키의 호출·크레딧 한도는 **따로 없다**(OT p.27). temperature는 OT가 "자유"라고 했으므로 §4의 거부 사례는 OpenAI 직접 호출 기준이고, 대회 프록시는 기동 시 확인한다. 공개 세트 크기는 OT에 없다(약 300은 제출당 가드 요청 수).

---

## 0. 요약: PRD에 넣을 결론

| # | 결론 | 근거 | 확신 | PRD 위치 |
|---|---|---|---|---|
| 1 | **현재 `llm.py`는 gpt-5 계열이 거부한다고 보고된 `temperature`·`max_tokens`를 보낸다.** OpenAI 직접 호출이면 400. OT는 temperature를 "자유"라고 했으므로 대회 프록시는 확인해야 하지만, 보내지 않는 쪽이 어느 경우든 안전하다 | §4 | 중(프록시 미확인) | GR-09, 코드 |
| 2 | 판정 호출은 **입력·출력 2회 병렬**. 1회 통합은 nano에서 간섭이 특히 크다 | §5 | 중상 | §4, ADR-0011 |
| 3 | `reasoning_effort: low` + `verbosity: low`. `minimal`은 판단 과제에서 정확도가 무너지고 `medium`은 느리다. minimal은 비교 실험으로만 | §4 | 중상 | GR-09 |
| 4 | JSON 스키마 순서는 **근거 → 범주 → 결정**. 답부터 내게 하면 작은 모델에서 정확도가 크게 떨어진다 | §1 | 중 | GR-04 |
| 5 | logprobs가 없으므로 확률 임계값이 없다. 조사 권고는 k=3 투표의 block 비율이었으나 **council에서 k=1로 바꿨다**(호출 수·키 회수 위험). 투표는 실험 플래그 | §3 | 높음 | GR-04, ADR-0011 |
| 6 | 근거 없는 구간은 코드가 원문에서 만든다. **council에서 문장 번호 선택을 기본으로**, LLM이 부분 문자열을 내고 코드가 정렬하는 방식은 구절 단위일 때로 정했다. 프롬프트만으로는 구간 F1이 낮다(최상위 모델도 42–51) | §2 | 방식 높음 / 정확도 낮음 | GR-07, ADR-0005 |
| 7 | `grounded = (unsupported == [])`로 두 필드가 모순되지 않게 한다 `[공개 세트 후]` | §2 | 중 | GR-07 |
| 8 | KB 검색은 **기존 문자 2-gram BM25 유지**. kiwipiepy·Mecab은 학습 모델·사전을 담고 있어 규칙 위반 | §6 | 높음 | GR-03 |
| 9 | 서버는 **FastAPI(`redirect_slashes=False`) + uvicorn 1 프로세스**. 동시성 게이트가 프로세스 단위라 워커를 늘리지 않는다 | §7 | 높음 | GR-01, GR-09 |
| 10 | OT상 키의 호출·크레딧 한도는 따로 없다(p.27). 남는 위험은 과한 호출 경고와 프록시의 실제 처리량이다 | §4 | 중 | §11-1, GR-09 |
| 11 | v4 자산 중 서버에 넣을 수 있는 것은 정규화 코드뿐. LLM이 초안을 쓴 데이터는 README에는 그렇게 적혀 있으나 레코드 값은 `source: "self"`다 | §8 | 높음 | GR-13 |
| 12 | `trace.py`의 원문 저장은 대회 로그 규칙과 정면 충돌한다 | §8 | 높음 | GR-11, 코드 |

---

## 1. 작은 LLM을 입력 판정기로 쓰기

**가드 모델의 설계 패턴(프롬프트 설계로만 참고)**
- Llama Guard: 번호 붙은 범주 목록과 "해서는 안 됨 / 해도 됨" 설명 → 대화 → 첫 줄 `safe/unsafe`, 둘째 줄 위반 범주. 범주별 2–4개 예시(위반 + 안전한 경계 사례)로 AUPRC 0.837 → 0.872. 범주가 많은 제로샷은 약하다고 경고 [1] **[F]**
- ShieldGemma: 입력과 출력에 다른 지시를 쓴다. 입력은 위반을 담거나 끌어내려는지, 출력은 생성한 내용이 위반인지 본다 [3] **[F]**. 우리 `input`/`output` 분리와 같은 구조다.
- OpenAI gpt-oss-safeguard 지침: 지시·정의·기준·예시 4부, 400–600토큰, 범주가 겹칠 때 우선순위 명시, "보통·일반적으로" 같은 완화어 금지, 정책을 하나 더할 때마다 정확도가 조금씩 떨어진다 [2] **[F]**. v4 PRD GR-04의 정책 프롬프트 원칙과 같다.

**출력 순서**
- 스키마 순서대로 생성되므로 답을 먼저 내게 하면 작은 모델에서 정확도가 크게 떨어지고, 근거를 먼저 내게 하면 자유 형식과 비슷해진다 [9] **[F]**. 근거 필드를 앞에 두면 +15–24%p라는 보고도 있다 [10] **[F]**.

**권고 [R]**
- 정책 프롬프트는 고정 접두로 앞에 둔다(캐시). 범주 10개의 정의와 block/allow 기준, 우선순위(예: `jailbreak_injection` > `harmful_content` > `pii_disclosure` > … > `low_signal_input`), 범주당 한국어 경계 사례 1–2개(개발 분할에서만, 팀이 직접 씀).
- 스키마: `{"evidence": str(≤2문장, 결정적인 학습자 문장 인용), "category": enum, "decision": enum}`.
- 학습자 발화 전체(“시스템 지시”처럼 보이는 문장 포함)를 구분자로 감싼 데이터로 다룬다.
- 범주가 decision을 결정적으로 정하면(교차표가 순수하면) decision은 코드로 계산한다 `[공개 세트 후]`.

## 2. 근거 없는 구간 탐지

**벤치마크 [F]**
- RAGTruth 구간 F1(프롬프트만): gpt-4-turbo 28.3, GPT-4o-mini(CoT) 33.1, GPT-5-mini 44.5, GPT-5 42.2, o3 51.2. 미세조정 14B 55–58, LettuceDetect-large 58.9 [12][13].
- "답변에 환각이 있는가"(예시 단위 F1 63–84)는 "어디인가"(구간 28–59)보다 훨씬 쉽다 [13].
- 다국어 대회(Mu-SHROOM) 상위 팀의 공통점: 몇 개 예시, 투표 앙상블, 퍼지 매칭으로 구간 정렬 [24]. **한국어 구간 단위 벤치마크는 찾지 못했다.**

**권고 [R]**
- 출력 판정은 "답변에서 그대로 복사한 부분 문자열 목록"을 내게 하고, 코드가 정규화(공백·문장부호 제거, NFC) 후 찾지 못하는 구간을 버리고, 채점 단위로 맞춘다(§6-4의 정렬 함수). council 뒤로는 문장 번호 선택이 기본이고 이 방식은 구절 단위일 때만 쓴다.
- 구간 단위(문장·절·구)는 공개 세트 정답 구간의 길이 분포로 정한다 `[공개 세트 후]`.
- 검색한 KB 섹션을 ID와 구분자로 감싸 넣고 "근거는 KB만, 일반 상식은 근거 아님"을 명시한다. 단, 자료와 모순되지 않는 일반 설명의 라벨은 `[공개 세트 후]`.
- 인사·되묻기·유도 질문 같은 비주장 문장은 검사에서 뺀다.
- `grounded = not unsupported`, `safe`는 같은 출력 판정 호출에서 같이 낸다(문맥 공유, 비용 작음).
- **기대치 [I]**: nano 프롬프트만으로 구간 F1 30–45. 점수는 검색 품질과 구간 후처리에서 나온다. **[측정]**

## 3. 작은 개발 세트로 block F1 맞추기

**사실 [F]**
- gpt-5 계열은 OpenAI 직접 호출에서 `logprobs`·`temperature`(기본값 외)·`top_p`를 받지 않는다 [8].
- 표본 일관성(여러 번 뽑아 비율)이 모델이 말로 낸 확신보다 보정이 낫다 [17]. 말로 낸 확신은 몇 개 값에 몰려 임계값 지표를 왜곡한다 [18].
- 최적화한 프롬프트는 학습 세트에서 5–20% 더 높게 나온다(과적합) [19][20].
- 같은 문항에서 두 설정을 비교할 때는 짝지은 차이의 표준오차를 쓰고, 한 대화에서 여러 문항이 나오면 대화 단위로 묶는다 [21].

**권고 [R]**
- (조사 권고, council에서 k=1로 바뀜) 입력 판정을 k=3으로 병렬 호출하고 block 표 비율을 점수로 쓴다. 범주는 최종 decision과 일치하는 표 중 다수결.
- 임계값(≥1/3, ≥2/3 …)은 교차검증 폴드에서 F1로 고른다.
- 절차: 범주 층화 + 대화 단위 분리, 30% 홀드아웃 잠금(최대 3회만 봄), 예시는 학습 폴드에서만, 프롬프트 변경은 McNemar로 기존보다 나을 때만 채택, block F1에 부트스트랩 1,000회 신뢰구간.
- **규모 [I]**: 공개 세트 크기는 OT에 없다. 약 300문항·block 40%라면 block F1 95% 신뢰구간이 ±0.05–0.07. 2–3%p 차이는 구별되지 않으니 쫓지 않는다.
- 투표 k는 비용·지연·한도와 맞바꾼다. (council 뒤) k=1을 기본으로 두고 "2표, 엇갈리면 1표 추가" 플래그와 비교 **[측정]**(ADR-0011).

## 4. gpt-5-nano 운영 사실

| 항목 | 값 | 출처 |
|---|---|---|
| 상태 | OpenAI 모델 페이지에 **Deprecated** 표시. 스냅샷 `gpt-5-nano-2025-08-07`, 컨텍스트 400k | [5] **[F]** |
| 기능 | 구조화 출력(json_schema), 함수 호출, 프롬프트 캐시. Chat Completions·Responses | [5] **[F]** |
| 받지 않는 파라미터(OpenAI 직접) | `temperature`(기본값 외), `top_p`, `logprobs`, `logit_bias`, `max_tokens`(→ `max_completion_tokens`). OT는 temperature를 "자유"라고 함 → 대회 프록시는 확인 | [8] **[F]** |
| 추론 수준 | minimal / low / medium(기본) / high. `verbosity` low/medium/high | [14][15] **[F]** |
| minimal의 정확도 붕괴 | 추론 과제에서 minimal 4.4% vs low 98.9% | [6] **[F, 블로그 1건]** |
| 지연(JSON 분류 약 360건) | medium p50 5.06s·p95 7.84s / low + verbosity low p50 2.56s·p95 3.87s. json_schema는 지연에 영향 없음 | [7] **[F, 블로그]** |
| 지시 따르기 | IFBench minimal 32.5% vs medium 65.9% | [30] **[F]** |
| 한국어 | KMMLU 69.6(mini 78.5), HAE-RAE 79.1 | [22] **[F]** |
| 한도(OpenAI 직접) | Build 등급 5,000 RPM / 2M TPM | [5] **[F]** |

**권고 [R]**
- `reasoning_effort="low"`, `verbosity="low"`, `response_format={"type":"json_schema","strict":true}`, `max_completion_tokens` ≥800(추론 토큰도 이 한도를 쓴다), `temperature` 보내지 않음.
- 호출당 타임아웃 약 12초, 요청 전체 마감 25초(council). 투표 플래그를 켤 때 빠진 표는 기권으로 처리.
- 기동할 때 대회 엔드포인트에 한 번 시험 호출해, 프록시가 `reasoning_effort`·`verbosity`·`json_schema`를 받는지 확인하고 안 받으면 그 파라미터를 뺀다(기존 `llm.py`의 단계적 강등 구조를 쓴다).
- **처리량 [I]**: 동시 64 × 요청당 2호출 × 호출당 입력 약 5k 토큰이면 분당 수백만 토큰이 될 수 있다. OT상 키의 호출·크레딧 한도는 따로 없지만(p.27), 과한 호출은 경고 뒤 키 회수 사유이고 프록시의 실제 처리량은 모른다. 채점 시간 제한(약 2–5분)과 함께 **[측정]**.
- **충돌 정리**: ① search-first는 `minimal`을, ② deep-research는 `low`를 권했다. 판단 과제에서 minimal의 정확도 붕괴 근거([6][16][30])가 더 직접적이라 **low를 기본**으로 하고 minimal은 지연 ablation으로만 잰다.

## 5. 호출 1회 통합 vs 2회 병렬

**사실 [F]**
- 코딩 기준 25개를 한 프롬프트에 쌓으면 대부분 모델은 1.6–6.1%p, **gpt-5-nano는 15.5%p** 떨어졌다. 원인은 길이가 아니라 다뤄야 할 기준 수 [4].
- OpenAI GPT-5 지침: "분리 가능한 과제는 나눌 때 최고 성능" [14]. gpt-oss-safeguard: 정책 추가마다 정확도 저하 [2].
- 같은 형태의 분류를 묶으면 약 3%p, 형태가 다른 과제(분류 + 선택)를 섞으면 10–32%p 손실 [32].

**판단 [I]**: 입력 판정(이력 전체의 의도 분류)과 출력 판정(KB 대조 구간 추출 + 안전)은 형태가 다른 과제라 간섭이 큰 쪽이다. 병렬이면 지연은 합이 아니라 느린 쪽이고, 공통 문맥을 두 번 보내는 비용은 작다.

**권고 [R]**: 2회 병렬, 각자 고정 접두(캐시). ① 입력 판정(council 뒤 k=1): 첫 학습자 발화 + 최근 6턴 + 정책 → evidence·category·decision. ② 출력 판정: 마지막 학습자 발화 + 후보 답변 + KB 섹션 → unsupported·safe. 결정적 신호(PII·빈 입력 등)는 LLM 앞에서 계산해 힌트로 넣는다(ADR-0002 v5).

## 6. 라이브러리·구현 (search-first)

### 6-1. 한국어 BM25 (GR-03)

| 이름 | 버전 / 날짜 | 라이선스 | 모델·사전 포함 | 판정 |
|---|---|---|---|---|
| 저장소 `BM25`(`eval/mock_tutor.py`, 문자 2-gram, k1=1.2, b=0.75) | — | 자체 | 없음 | **채택** |
| rank_bm25 | 0.2.2 / 2022-02 | Apache-2.0 | 없음 | 가능, 2022년 이후 정체 |
| bm25s | 0.3.16 / 2026-10-28 | MIT | 없음(선택 의존성 제외) | 가능, 규모에 비해 과함 |
| kiwipiepy | 0.24.1 / 2026-10-20 | Apache-2.0 | **있음**: 필수 의존성 `kiwipiepy_model`이 학습 언어모델(`cong.mdl` 등)과 사전 파일을 담음 | **불가** |
| konlpy + Mecab | — | GPL-3 / mecab-ko-dic | **있음**: 세종 말뭉치 기반 사전(약 346MB), Kkma는 jar 안에 사전 | **불가** |
| soynlp | 0.0.493 / 2019-08 | 미확인 | 없음(우리 말뭉치에서 비지도 학습) | 규칙상 가능, 방치됨·소규모 문서에 약함 |

**권고 [R]**: 새 의존성 없이 기존 2-gram BM25를 서버 모듈로 옮긴다. 조사·어미 변화는 2-gram이 흡수한다 **[I]**. 3-gram 추가, 영문·파이썬 식별자는 공백 단위 토큰 추가를 ablation으로 본다. 색인은 기동(lifespan) 때. 임베딩 검색과의 혼합은 공개 세트의 `tutor.jsonl` 근거 섹션으로 recall@k를 비교해 정한다 **[측정]**.

### 6-2. 서버 (GR-01, GR-09)
- FastAPI 0.162.2(MIT). 기본값 `redirect_slashes=True`가 `/guard` ↔ `/guard/` 307을 만든다 → **`redirect_slashes=False`**, 문서 경로 끔 [F].
- uvicorn 0.54.0, **워커 1개**. 일이 I/O 대기라 이벤트 루프 하나로 64개를 처리하고, `llm.py`의 AIMD 게이트는 프로세스 단위다 **[I]**. `--limit-concurrency`는 503을 내므로 설정하지 않거나 64보다 크게.
- OpenAI SDK 기본값은 재시도 2회·읽기 타임아웃 600초 [F] → `AsyncOpenAI(max_retries=0)`(기존 `llm.py`와 같음), 연결 풀 128/64, 호출별 타임아웃은 남은 마감에서 계산.
- 핸들러 전체를 `asyncio.timeout(25초)`로 감싸고, 인증 실패를 뺀 모든 예외(검증 오류 422 기본 포함)를 200 기본값 응답으로 바꾸는 예외 처리기를 둔다. 본문 크기는 직접 제한한다(uvicorn에 본문 크기 옵션이 있는지 미확인).
- BM25는 수백 섹션에서 1ms 미만이라 인라인 호출. 5ms를 넘으면 `asyncio.to_thread`.

### 6-3. 구조화 출력 (GR-04, GR-07)
- Chat Completions + `json_schema` strict(기존 `llm.py`에 있음)를 유지한다. instructor(1.17.0)는 재시도·파싱이 `llm.py`와 겹치고 기본 재시도가 예산과 충돌하므로 쓰지 않는다 **[R]**.
- 어떤 서드파티 프록시는 `verbosity`를 전달하지 않는다고 문서화돼 있다 [F]. 대회 프록시는 **[측정]**.

### 6-4. 구간 정렬 (GR-07)
rapidfuzz 3.14.6(MIT, 모델 없음)의 `partial_ratio_alignment`를 마지막 단계로 쓴다. 순서: 원문 그대로 찾기 → 공백·문장부호를 지운 정규화본에서 찾기 → rapidfuzz(정규화본, 컷오프 ≥90). 정규화본의 위치를 원문 위치로 되돌려 **항상 원문 부분 문자열**을 낸다. 64자를 넘는 구간은 rapidfuzz가 최적을 보장하지 않으므로 문장 경계로 먼저 나눈다 [F]. 한국어 띄어쓰기·문장부호가 다른 구간으로 테스트 하나를 둔다.

## 7. 기존 조사(R1–R11)의 유효 범위

| 문서 | v5에서 | 쓸 수 있는 부분 |
|---|---|---|
| R1 | 일부 | 겹친 방어, 간접 인젝션 원리. 분류기·카나리아 실물은 불가 |
| R2 | 서버 무관 | 외부 데이터셋은 서버 불가, 서버 밖 평가도 운영진 확인 전에는 쓰지 않음 |
| R3 | 일부 | S0–S14와 교육 원칙을 경계 사례 작성에 참고(PRD 부록 B) |
| R4 | 대부분 폐기 | 한국어 PII·가드·임베딩 모델 선정은 모델 파일 금지에 걸림. 정규식·체크섬 PII만 |
| R5 | 대부분 유효 | 우회 기법과 정규화 사양(GR-02·GR-06 근거). 쓸 수 없는 부분: N5 혼동 사전을 Unicode `confusables.txt`에서 가져오는 것, N9 욕설 초성 사전을 외부 목록에서 가져오는 것(둘 다 팀이 직접 씀, OT p.25), 분류기·canary 부분 |
| R6 | 폐기 | 학습 분류기 불가. 표본 수·임계값 통계만 참고 |
| R7 | 일부 | 구간 추출, 부분 문자열 확인, 3-gram 사전 필터, 문장 단위 원칙. 수리·재생성은 폐기 |
| R8 | 일부 | 지연 예산, 장애 기본값, 표본 수 통계. 로그·관측 부분은 폐기 |
| R9 | 일부 | 위기 신호 어휘 설계 원칙. 위기 응답·교사 알림·세션 요건은 가드 범위 밖 |
| R10 | 일부 | 병렬 호출 근거. 다른 계열 판정 모델 전제는 폐기 |
| R11 | 일부 | 지연 줄이기, 장애 처리, 부하 시험. Jev 역할 분담은 폐기 |

R 문서의 수치는 대부분 다른 모델 기준이라 nano에서 다시 잰다 **[측정]**.

## 8. v4 자산 출처 점검 (GR-13)

**서버에 넣을 수 있는 v4 데이터성 자산은 정규화 코드(`preprocess.py`)의 자모 표뿐이다**(코드 재사용은 아래 지도). v4 위기·인젝션 어휘는 파일로 존재하지 않으므로 GR-06 목록은 팀이 처음부터 쓴다. 다른 LLM이 만든 단어 목록은 넣지 않는다(팀 방침. OT p.25는 규칙 안의 예시를 팀 작성으로 보지만, 별도 목록·데이터 파일은 외부 데이터다).

| 자산 | 용도 | 출처 | 판정 |
|---|---|---|---|
| `src/guardrail/preprocess.py`(자모 변환표) | 정규화 | 유니코드 사실 기반 팀 작성(R5 참고 코드를 옮겨 고침) | 서버 가능. 출처를 provenance에 적음 |
| `resources/crisis_resources.yaml` | 위기 연락처 | 공공 번호 + 팀 문구, `source` 필드가 기사 인용 | 가드 응답에 문구가 없어 v5 가드에서는 불필요. `/tutor` escalate 문구로 쓴다면 팀이 직접 확인해 씀 |
| `prompts/attack_judge.md` | v4 공격 성공 판정 | 팀 작성(AI 코딩 도구 보조) | 평가 전용, S0–S14 기준이라 v5에선 폐기 후보 |
| `data/seed/smoke_benign.jsonl`(50) | 정상 스모크 | Sonnet 초안 + 본인 검토(`data/README.md`에 명시). 레코드 값은 `source: "self"` | 평가 전용. provenance에 LLM 초안으로 적음 |
| `smoke_attack.jsonl`(50)·`smoke_crisis.jsonl`(30), 로컬 전용 | 공격·위기 스모크 | 같음(LLM 초안) | 평가 전용, 서버 금지 |
| `data/seed/textbook_placeholder.jsonl`(38절) | 모의 튜터 교재 | Sonnet 초안 | 폐기(`kb/`로 대체) |
| `data/seed/grounding/inject_prompt.md` | 근거 이탈 주입 데이터 생성 | 팀 작성 프롬프트, 산출물은 LLM 생성 | 폐기 후보 |
| `configs/default.yaml`·`unguarded.yaml` | v4 설정(`typesafe-ai/jev-1.14.0` 포함) | 팀 작성 | 다시 씀(Jev 삭제, nano·임베딩만) |
| `results/*-fake.*`, `BASELINE_BLOCKED.md` | 가짜 응답 하네스 시험 | 코드 생성 | 폐기 가능 |
| 외부 세트(wildguardmix, MultiJail 등) | — | 저장소에 본문 없음 | 반입하지 않음 |

**의존성**: `openai`·`pydantic`·`pyyaml`은 모델·사전 없음, 문제없음. `llm.py`는 `GUARD_LLM_BASE_URL`을 읽으므로 대회 변수(`OPENAI_BASE_URL`·`OPENAI_MODEL`·`EMBEDDING_*`)로 바꾼다. 문서에만 있던 KURE-v1·LogisticRegression·Jev는 모두 금지.

**코드 재사용 지도**(다음 플랜에서 사용)

| 모듈 | 판정 |
|---|---|
| `preprocess.py` | 그대로 재사용 |
| `llm.py` | 재시도·AIMD 게이트·예산·스키마 파싱 재사용. `temperature`·`max_tokens` 제거, `MAX_ATTEMPTS = 3` → 호출마다 재시도 1회(게이트 대기도 마감에 포함), 역할 5종·가짜 응답·다른 계열 가정 삭제, 임베딩 호출 추가 |
| `config.py` | 환경 치환·해시·경로 재사용, v4 단계·정책 항목 정리 |
| `types.py` | `/guard` 요청·응답 스키마로 교체(`Session`·`Chunk`·`CrisisTier` 폐기) |
| `pipeline.py` | 폐기, HTTP 핸들러로 새로 씀 |
| `trace.py` | **원문 저장 경로 때문에 폐기 또는 대폭 축소**(비밀 스크럽만 재사용) |
| `eval/datasets.py`·`metrics.py`·`stats.py` | 로더·분할·통계 재사용, 채점은 `score_local.py` 기준으로 다시 씀 |
| `eval/run.py`·`mock_tutor.py`(BM25 제외)·`build_grounding.py` | 폐기 |

## 9. 남은 공백

- gpt-5-nano를 가드·구간 판정기로 쓴 공개 정확도는 찾지 못했다. 위 기대치는 모두 추론이고 공개 세트에서 잰다.
- 한국어 구간 단위 환각 벤치마크는 없다.
- 지연 수치는 블로그·집계 사이트 출처다. 대회 프록시의 지연·한도·파라미터 지원은 11/10 이후 실측.
- 출처 25(F1 최적 임계값 이론)는 원문을 열지 않았다. soynlp·bm25s 라이선스, uvicorn 본문 크기 옵션은 미확인.
- 로컬 전용 스모크 파일 본문, `tests/` 내용은 점검하지 않았다.

## 출처

1. Llama Guard (2023-12) https://arxiv.org/html/2312.06674 · PurpleLlama #7 https://github.com/meta-llama/PurpleLlama/issues/7
2. OpenAI gpt-oss-safeguard guide (2025-10) https://developers.openai.com/cookbook/articles/gpt-oss-safeguard-guide
3. ShieldGemma (2024-07) https://arxiv.org/html/2407.21772v2
4. LLM annotation costs (2026-04) https://arxiv.org/html/2604.03684
5. OpenAI GPT-5 nano 모델 페이지 https://developers.openai.com/api/docs/models/gpt-5-nano
6. OpenAI 모델 10종 비교 (2026-05-15) https://jaesolshin.com/posts/openai-models-comparison/
7. Qiita gpt-5-nano 지연 (2026-03-13) https://qiita.com/testuse80712458/items/771341c162608df7b6bd
8. microsoft/agent-framework #1700 (2025-10-26) https://github.com/microsoft/agent-framework/issues/1700
9. Structure Tax (AACL 2026) https://aclanthology.org/2026.aacl-long.209/
10. Your Prompt Is Not the Only Prompt (2026-08) https://arxiv.org/html/2608.08254
12. RL4HS (2025-10) https://arxiv.org/html/2510.02173
13. LettuceDetect (2025-02) https://arxiv.org/html/2502.17125
14. OpenAI Using GPT-5 https://developers.openai.com/api/docs/guides/latest-model/gpt-5
15. OpenAI Cookbook GPT-5 new params https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_new_params_and_tools
16. Reasoning defaults (2025-10-05) https://www.s-anand.net/blog/the-non-obvious-impact-of-reasoning-defaults/
17. Sample Consistency (2024-02) https://arxiv.org/html/2402.13904v2
18. Confidence sparsity (2026-08) https://export.arxiv.org/pdf/2608.04899
19. Prompt optimization overfitting https://hackernoon.com/how-overfitting-affects-prompt-optimization
20. Prompt optimisation as search (2025-11) https://arxiv.org/pdf/2511.18619
21. Adding Error Bars to Evals (2024-11) https://arxiv.org/html/2411.00640
22. Korean LLM benchmark (2025-08-11) https://github.com/daekeun-ml/evaluate-llm-on-korean-dataset
24. Mu-SHROOM overview https://aclanthology.org/2025.semeval-1.322/
25. Thresholding Classifiers to Maximize F1 (2014, 미열람) https://arxiv.org/abs/1402.1892
30. UnifyBench effort table (2026-10-31) https://unifybench.ai/efforts?model=gpt-5-nano
32. Multi-problem evaluation (2025) https://aclanthology.org/2025.insights-1.12.pdf
- 라이브러리: PyPI JSON API, github.com/bab2min/kiwipiepy, konlpy.org/ko/latest/data, github.com/lovit/soynlp, github.com/xhluca/bm25s, fastapi.tiangolo.com/reference/fastapi/, github.com/openai/openai-python `_base_client.py`, rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html
