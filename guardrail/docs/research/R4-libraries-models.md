# R4. 라이브러리·모델 Search-First 평가

- 작성일: 2026-10-29
- 대상: Python 3.12 가드레일 패키지 (한국어 RAG 학습 튜터), LLM은 Elice ML API 경유, GPU 없음(CPU 전용), 대회 환경 패키지 제한 가능
- 방법: PyPI / GitHub / 공식 문서 / Hugging Face 모델 카드 열람(설치·다운로드 없음). 점수 축은 기능, 유지보수(최근 릴리스), 라이선스, 의존성 무게, 한국어 지원, CPU 가능성이다.
- 표기: Adopt = 그대로 사용 / Extend = 채택 후 확장 / Compose = 여러 개 조합 / Build = 직접 구현. "추정"은 문서 근거가 없는 값이라 실측해야 한다.

## 요약 결정표

| 필요 기능 | 선택 | 결정 | 이유 | 패키지 제한 시 대안 |
|---|---|---|---|---|
| LLM 호출 | Elice ML API + `openai` SDK(또는 `httpx`) | Adopt (조건부) | 상용 모델(GPT·Claude)을 단일 크레딧으로 제공한다는 점은 확인됨. OpenAI 호환 여부는 공개 문서에서 확인되지 않음 | `httpx`/`urllib`로 REST 직접 호출 |
| 한국어 PII | 자체 regex + 체크섬 (`korean-pii` 규칙 참고) | Build | 주민번호·외국인번호 체크섬, 전화, 계좌(문맥), 학번은 50~100줄 정도의 stdlib `re`로 충분함. Presidio는 spaCy가 무겁고 학번·전화(KR)가 없음 | 동일(의존성 0) |
| 프롬프트 인젝션 분류 | 자체 경량 분류기(임베딩 + LR) + LLM 판정. 오프라인 비교 대상으로 Kanana Safeguard-Prompt 2.1B | Compose | Kanana-Prompt는 한국어 F1 0.844(자체 보고)지만 2.1B라 CPU에서 수 초가 걸릴 것으로 추정됨. Prompt Guard 2 86M은 한국어가 평가 언어에 없음 | regex 휴리스틱 + LLM-as-judge(API) |
| 유해성 판정 | LLM-as-judge(Elice API, JSON 출력) | Adopt(API) | Kanana Safeguard 8B·Siren 8B는 CPU 실시간 사용이 어려움. Qwen3Guard 0.6B는 119개 언어를 주장하지만 한국어 수치가 없음 | Qwen3Guard-Gen-0.6B(CPU 오프라인 평가용) |
| 임베딩(probe/관련도) | `nlpai-lab/KURE-v1` (또는 `BAAI/bge-m3`) | Adopt | MIT, 1024차원, 한국어 검색 벤치에서 bge-m3·e5·OpenAI-3-large보다 높음(저자 보고). 0.6B라 CPU 배치 처리가 가능함(추정) | `multilingual-e5-small`(소형) 또는 API 임베딩(Elice 제공 여부 미확인) |
| 근거성(groundedness) | LLM 주장 분해 + 문맥 대조(API), RAGAS 방식을 직접 구현 | Build(소형) | MiniCheck·HHEM-2.1-open·AlignScore는 영어 전용. LettuceDetect 다국어판에 한국어 없음. HHEM의 한국어 지원은 상용 2.3에만 있음 | 임베딩 코사인 + 어휘 중첩 휴리스틱 |
| 비교용 프레임워크 | NeMo Guardrails(주), Guardrails AI(부) | Adopt(비교 전용) | NeMo는 3.12를 지원하고 `engine: openai` + `base_url`로 커스텀 엔드포인트를 연결할 수 있음. LlamaFirewall은 torch 의존이고 2025-05 이후 릴리스가 없음 | 비교 생략 사유를 문서화 |
| 레드팀 | garak(주) + 자체 한국어 시드셋 | Compose | Python, 3.12 지원, OpenAICompatible 제너레이터. 번역은 m2m100 등 로컬 모델이 필요해 무거움. promptfoo는 Node 기반 | PyRIT(`OpenAIChatTarget` endpoint 지정), 자체 스크립트 |
| 분류기 + 보정 | scikit-learn LogisticRegression + numpy 분위수 직접 계산 | Build(10줄) | split conformal 임계값은 `np.quantile` 한 줄로 끝남. MAPIE(BSD-3, 2026-10 릴리스)는 검증용으로만 사용 | numpy만으로 LR까지 구현 가능 |
| 유니코드 정규화 | `unicodedata.normalize("NFKC")` + 자체 자모 조합(산술식) + 소형 혼동문자 표 | Build | stdlib로 충분함. `jamo`는 2017년, `hangul-utils`·`soynlp`는 GPL에 2019~2020년이 마지막 릴리스, `confusable_homoglyphs`는 GitHub 저장소가 아카이브됨 | 동일 |

---

## 1. Elice ML API

| 항목 | 확인 결과 | 근거 |
|---|---|---|
| 제공 모델 | 모델 라이브러리가 OpenAI·Anthropic·Google 상용 API를 통합 제공. GPT-5, Claude Sonnet 사용 사례가 언급됨. 오픈소스로 gpt-oss, HyperCLOVA X SEED 호스팅 | Elice 브런치 글 |
| 과금 | 상용 모델은 "단일 크레딧"으로 과금. 공개 가격표에는 서버리스 오픈 모델만 있음: HyperCLOVAX-SEED-Text-Instruct-1.5B $0.015/$0.025 (1M 토큰 입력/출력), 0.5B $0.010/$0.020, Trillion-7B-preview $0.025/$0.040, Whisper v3t $0.00345/분 | elice.io/cloud/pricing |
| OpenAI 호환 | **공개 문서로 확인 불가.** REST API이고 엔드포인트가 자동 생성된다는 설명만 있음 | ML API 제품 페이지 |
| JSON mode / `response_format` | 확인 불가 | — |
| 임베딩 엔드포인트 | 확인 불가(언급 없음) | — |
| Rate limit | 확인 불가 | — |

판단: 공개된 API 레퍼런스(base URL, 모델 ID, 스키마)는 찾지 못했다. 코드는 `base_url`과 `model`을 설정으로 빼서 OpenAI SDK 형식을 기본으로 하되, 호출부는 하나의 함수(`llm_call(messages, json=True)`)로 묶어 형식 차이를 한 곳에서 흡수한다. JSON 모드가 없을 경우를 대비해 "JSON만 출력" 지시, `json.loads` 실패 시 1회 재시도, 정규식으로 `{...}` 추출하는 폴백을 기본 경로에 둔다.

## 2. 한국어 PII 탐지

| 후보 | 기능 | 유지보수 | 라이선스 | 의존성 | 한국어 | CPU | 평가 |
|---|---|---|---|---|---|---|---|
| Microsoft Presidio (`presidio-analyzer` 2.2.366, 2026-10-27) | KR_RRN, KR_FRN, KR_BRN, KR_DRIVER_LICENSE, KR_PASSPORT. 전화는 `supported_regions` 확장으로 가능 | 활발함 | MIT | 무거움(spaCy NLP 엔진이 기본) | 부분 지원(학번·계좌·KR 전화 없음) | O | 과함 |
| `korean-pii` 0.2.0 (2026-07-28) | 주민·외국인번호 체크섬, 전화, 카드(Luhn), 이메일, 계좌·여권(문맥), 운전면허, API 키 | 신생(저장소 별 수 적음) | MIT | 0 | O | O | 참고 구현으로 좋음. 학번 없음 |
| `kpii` 0.1.0 (2025-06-12) | 주민번호, 전화, 이메일, 카드 | 단일 릴리스 | MIT | 경량 | O | O | 범위 좁음 |
| HF `id4thomas/ko-pii-detector-ax-crf`, BCCard MoAI-Privacy-Filter | NER(이름·주소 등) | 모델 카드 | 카드별로 다름 | torch | O | 느림(추정) | 범위 밖. 이름 탐지가 필요해질 때 검토 |

결정은 **Build**다. 정형 식별자는 regex에 체크섬을 더하는 것이 정답이고, BCCard 카드도 정형 ID에는 결정적 regex를 백스톱으로 권장한다. 주민번호 체크섬(가중치 2~9,2~5 → `(11 - s%11)%10`), 하이픈·공백·전각 숫자 변형(NFKC 이후 적용), 학번(학교별 형식이므로 설정값으로 둠)과 계좌는 "계좌/입금/은행명" 문맥 키워드가 있을 때만 잡는다. 2020년 10월 이후 발급된 주민번호는 체크섬이 맞지 않을 수 있으므로, 체크섬 실패는 "탐지 안 함"이 아니라 "신뢰도 낮음"으로 처리한다. 대회에서 대체 라이브러리 비교가 필요하면 `korean-pii`를 오프라인 비교 대상으로 쓴다.

## 3. 한국어 안전·가드 모델

| 모델 | 크기 | 라이선스 | 한국어 | 탐지 범위 | CPU 실시간 | 비고 |
|---|---|---|---|---|---|---|
| Kanana Safeguard 8B | 8B (BF16) | Apache-2.0 | 최적화됨 | S1~S7 유해성(입력·응답) | X | 자체 보고 F1 0.946 (LlamaGuard3 8B는 0.540) |
| Kanana Safeguard-Siren 8B | 8B | Apache-2.0 | 최적화됨 | I1 성인인증, I2 전문조언, I3 개인정보, I4 지재권 | X | F1 0.926 (GPT-4o 0.862) |
| Kanana Safeguard-Prompt 2.1B | 약 2B | Apache-2.0 | 한·영 | A1 인젝션, A2 프롬프트 유출 | △ (출력 1토큰이라 prefill이 비용 대부분. 추정 수 초/건, fp32 RAM 약 8GB) | F1 0.844, P 0.968, R 0.748. Prompt Guard 2 86M은 0.751 |
| Llama Prompt Guard 2 86M | 86M (mDeBERTa-base) | Llama 4 Community | 평가 언어는 en/fr/de/hi/it/pt/es/th. **한국어 없음** | benign/malicious | O | 문맥 512토큰 |
| Llama Prompt Guard 2 22M | 22M (DeBERTa-xsmall) | Llama 4 Community | 영어 위주 | 동일 | O | 다국어 성능이 낮음 |
| Llama Guard 3 1B | 1B | Llama 3.2 Community(게이트) | 8개 언어, **한국어 없음** | S1~S13 | △ | 힌디어 F1 0.68처럼 비주력 언어에서 약함 |
| Qwen3Guard-Gen-0.6B | 약 0.6~0.8B | Apache-2.0 | 119개 언어 주장, **한국어 수치 미공개** | Safe/Controversial/Unsafe, 9개 범주(PII·jailbreak 포함) | O~△ (추정 1초 안팎) | Stream 변형 있음. 4B·8B도 있음 |
| ShieldGemma 2B | 2B | Gemma(게이트) | **영어 전용** | 4개 범주 | △ | 제외 |
| gpt-oss-safeguard-20b | 21B (활성 3.6B) | Apache-2.0 | 미기재 | 사용자 정책 기반 추론 | X (16GB VRAM 기준) | Elice가 gpt-oss를 호스팅하므로 API로 쓸 수 있는지 문의 필요 |
| Naver / LG / KT / Upstage 가드 | — | — | — | — | — | 공개 가드 모델을 찾지 못함. HF 커뮤니티 모델(`FrameByFrame/guardrail-korean-gemma-4-E2B`)은 출처 신뢰도가 낮아 제외 |

결정은 **Compose**다. 런타임은 (a) regex·정규화 휴리스틱, (b) 임베딩 + LR 경량 분류기(밀리초 단위), (c) 애매한 구간만 Elice LLM-as-judge(JSON 판정)로 구성한다. Kanana-Prompt 2.1B와 Qwen3Guard-0.6B는 **오프라인 벤치마크 비교 대상**으로만 쓴다. CPU에서 실측해 p50이 1초 이하면 (c)를 대체하는 후보로 승격한다. 참고로 mozilla.ai `any-guardrail`이 Kanana Safeguard 래퍼를 제공하므로, 비교 실험 코드를 줄일 수 있다.

## 4. 한국어 임베딩

| 모델 | 크기·차원·길이 | 라이선스 | 한국어 근거 | CPU | 평가 |
|---|---|---|---|---|---|
| `nlpai-lab/KURE-v1` | 약 0.6B (bge-m3 미세조정), 1024, 8192 | MIT | MTEB-ko-retrieval 8개 세트 평균 Recall@1 0.526 / NDCG@1 0.606. bge-m3 0.518/0.598, me5-large 0.501/0.577, OpenAI 3-large 0.449/0.517 (저자 보고) | △~O (짧은 문장 배치, 추정 수십~수백 ms/건) | **1순위** |
| `BAAI/bge-m3` | 약 0.57B, 1024, 8192, dense/sparse/ColBERT | MIT | KURE보다 근소하게 낮음. Ko-StrategyQA 약 79.4 vs KURE 약 80.0(제3자 표). 차이 1점 미만 | 동일 | 동급 대안 |
| `intfloat/multilingual-e5-large` / `-small` | large 약 0.56B / small 약 118M | MIT | KURE 표에서 large가 2~3점 낮음 | small은 O | 저사양 폴백(`query:`/`passage:` 접두어 필요) |
| `jhgan/ko-sroberta-multitask` | 약 0.1B, 768, **128토큰** | 카드에 미기재 | KorSTS Spearman 85.6 (STS용이고 검색용은 아님) | O | 길이 제한 때문에 RAG 관련도 용도로는 부적합 |
| Upstage Solar Embedding | API (passage/query 분리) | 상용 | 한국어 강세(공식 수치는 미확인) | — | Elice 경유 불가로 보여 외부 키가 필요함. 제외 |
| OpenAI text-embedding-3-large/small | API | 상용 | KURE 표에서 가장 낮음 | — | Elice가 임베딩을 프록시하는 경우에만 폴백 |

결정은 **Adopt KURE-v1**이다(`sentence-transformers` 또는 `transformers` 직접 사용). probe 학습용 특징과 검색-응답 관련도 점수를 같은 모델로 계산해 캐시한다. CPU 지연은 실측이 필요하다. 느리면 ONNX/int8로 변환하거나 e5-small로 내린다. torch가 금지된 환경이면 `fastembed`(ONNX, NeMo가 이미 의존함)로 bge-m3 계열을 돌리는 방안을 검토한다.

## 5. 근거성 / 환각 탐지

| 후보 | 크기 | 라이선스 | 한국어 | CPU | 유지보수 | 평가 |
|---|---|---|---|---|---|---|
| MiniCheck (roberta-L, deberta-v3-L 약 0.4B, flan-t5-L 0.8B) | 0.4~0.8B | 저장소 Apache-2.0 | 영어 | △ | 마지막 공지가 2024-09. PyPI 없음(git 설치) | 한국어 미지원 |
| Bespoke-MiniCheck-7B | 8B | **CC BY-NC 4.0** | 영어 | X | — | 라이선스·CPU 모두 탈락 |
| Vectara HHEM-2.1-Open | 0.1B (flan-t5-base) | Apache-2.0 | **영어**(한국어는 상용 HHEM-2.3에만 있음) | O (2k 토큰 약 1.5초, 600MB 미만) | — | 한국어 탈락. 번역 후 적용하는 실험만 가능 |
| LettuceDetect 0.2.2 (2026-07-05) | 17M~610M, EuroBERT 다국어 | MIT | 7개 언어(en/de/fr/es/it/pl/zh), **한국어 없음** | O(인코더) | 활발함 | 한국어 탈락. `method="llm"`은 OpenAI 키를 전제함 |
| AlignScore | RoBERTa | 미확인 | 영어(러시아어는 별도 재학습이 필요했음) | △ | 2023 | 탈락 |
| RAGAS 0.4.3 (2026-01) faithfulness | — (LLM 사용) | Apache-2.0 | LLM 성능에 따름 | API | 활발함, API 변경 잦음 | 알고리즘만 차용 |
| **LLM 주장 분해 + 문맥 대조 (자체)** | — | — | O | API | — | **채택** |

결정은 **Build(소형)**다. RAGAS와 같은 방식으로 (1) 응답을 원자 주장으로 분해하고 (2) 각 주장의 supported/unsupported를 JSON으로 받은 뒤 (3) 지지 비율을 계산한다. 이를 Elice API 1~2회 호출로 구현하면 프롬프트 포함 약 60줄이다. RAGAS 패키지를 쓰지 않는 이유는 의존성(langchain 계열)과 잦은 API 변경 때문이다. API 비용·지연을 줄이려고 1차 필터로 KURE 코사인(주장과 청크 사이 최댓값)을 쓰고, 임계값 아래인 주장만 LLM으로 판정한다.

## 6. 가드레일 프레임워크 (비교 실험용)

| 항목 | NeMo Guardrails 0.24.1 (2026-09-16) | Guardrails AI 0.11.0 (2026-08-14) | LlamaFirewall 1.0.3 (2025-05-29) |
|---|---|---|---|
| Python 3.12 | O (3.10~3.13) | O (3.10~3.13) | 3.10 이상(메타데이터 미기재) |
| 설치 무게 | 중간: fastembed, onnxruntime, lark, pydantic 등 약 21개. torch 없음 | 가벼움(코어). 검증기는 PyPI `guardrails-ai-<name>` 패키지로 바뀜(0.11부터 Hub CLI·토큰·원격 추론 종료, 2026-08-06) | 무거움: **torch, transformers**, codeshield, openai |
| 커스텀 LLM 엔드포인트 | `engine: openai` + `parameters.base_url`, `api_key_env_var` (공식 문서) | 커스텀 LLM 호출 가이드 있음(LiteLLM 계열) | AlignmentCheck는 Together API 키를 전제함. 커스텀 엔드포인트 문서 없음 |
| 한국어 | Colang 플로우와 프롬프트를 한국어로 직접 작성해야 함. 내장 self-check 프롬프트는 영어 | 검증기 대부분이 영어 모델 | PromptGuard 2는 한국어 미평가 |
| 라이선스 | Apache-2.0 | Apache-2.0 | PyPI에 미기재(LICENSE 파일만 있음) |
| 평가 | **비교 기준선 1순위**: self check input/output rail을 한국어 프롬프트로 구성 | 2순위: `regex_match` 등 단순 검증기로 비교 | 탈락(무게, 유지보수 정체, HF 게이트 모델) |

결정은 **Adopt(비교 전용)**이다. 본 패키지에는 의존성으로 넣지 않고, `bench/` 같은 별도 extras에서만 설치한다.

## 7. 레드팀 도구

| 도구 | 런타임 | 최근 릴리스 | 라이선스 | 커스텀 OpenAI 호환 | 한국어/번역 | 평가 |
|---|---|---|---|---|---|---|
| garak 0.17.1 (2026-10-16) | Python 3.11~3.14 | 활발함 | Apache-2.0 | `OpenAICompatible` 제너레이터(uri, `--target_name`), REST 제너레이터 | `langproviders`(m2m100 로컬 또는 DeepL·Riva)로 프로브 번역. 다만 문장 처리가 "en"에 강하게 묶여 있음 | **주 도구**. 번역은 사전 생성한 한국어 프롬프트셋으로 대체 |
| PyRIT 1.1.0 (2026-09-04) | Python 3.10~3.14 | 활발함 | MIT | `OpenAIChatTarget(endpoint=..., api_key=..., model_name=...)` | 번역 컨버터 존재(LLM 기반. 확인 필요), 다중 턴 오케스트레이터 | 보조. 다중 턴 공격 시나리오용 |
| DeepTeam 1.0.10 (2026-10-16) | Python 3.9~3.13 | 활발함 | Apache-2.0 | DeepEval 커스텀 LLM 래퍼 | "덜 쓰이는 언어로 번역" 공격 있음 | 선택. DeepEval 의존성이 무거움 |
| promptfoo | **Node.js**(npx) | 활발함 | MIT | `apiBaseUrl` + `apiKeyEnvar` | 플러그인 기반 | 대회 환경이 Python 전용이면 제외. 로컬 CI에서만 사용 |

결정은 **Compose**다. 공격 시드는 한국어 자체 데이터셋(JSONL)으로 만들고, garak은 OpenAICompatible 타깃으로 기본 프로브 스윕만 돌린다. 한국어 변형(자모 분리, 초성, 전각, 영·한 혼용, 번역)은 우리 정규화 모듈의 역함수로 생성한다. 이렇게 하면 같은 코드로 공격과 방어를 함께 검증할 수 있다.

## 8. 분류기 + 보정(conformal)

| 후보 | 버전·날짜 | 라이선스 | 의존성 | 평가 |
|---|---|---|---|---|
| scikit-learn `LogisticRegression` (+ `CalibratedClassifierCV`) | — | BSD-3 | numpy, scipy | **채택** |
| MAPIE 1.6.0 (2026-10-19) | v1에서 API가 크게 바뀜 | BSD-3 | numpy, scikit-learn 1.4 이상, scipy | 결과 교차검증용 |
| crepes 0.9.1 (2026-06-12) | Mondrian(클래스 조건부) 지원 | BSD-3 | numpy, pandas, scipy | 선택 |
| numpy 직접 계산 | — | — | numpy | **채택** |

결정은 **Build(약 10줄)**다. split conformal에서 비적합 점수는 `s = 1 - p̂(y_true)`이고, 임계값은 `q = np.quantile(s_cal, np.ceil((n+1)*(1-α))/n, method="higher")`이다. 클래스 조건부(Mondrian)로 바꿔도 클래스별로 같은 식을 반복하면 된다. MAPIE를 쓰면 v1 API 변경에 노출되므로 테스트에서 수치 일치만 확인한다.

## 9. 유니코드 정규화 / 난독화 대응

| 후보 | 상태 | 라이선스 | 평가 |
|---|---|---|---|
| `unicodedata` NFKC (stdlib) | — | PSF | **채택**: 전각, 호환 자모(ㄱ U+3131 등), 원문자 정규화 |
| `confusable_homoglyphs` 3.3.1 (2024-01-30) | GitHub 아카이브(sr.ht로 이전) | MIT | 탈락. 필요한 키릴·그리스·라틴 혼동 수십 개는 dict로 직접 둠 |
| `jamo` 0.4.1 (2017-11) | 베타, 장기 미갱신 | Apache-2.0 | 탈락. 조합은 산술식 한 줄 `0xAC00 + (cho*21 + jung)*28 + jong` |
| `hangul-utils` 0.4.5 (2020-06) | 미갱신 | GPL | 탈락(라이선스) |
| `soynlp` 0.0.493 (2019-08) | 미갱신 | GPLv3 | 탈락(라이선스, scikit-learn·psutil 의존) |

결정은 **Build**다. 파이프라인은 NFKC → 제로폭 문자·제어문자 제거(U+200B~U+200D, U+2060, U+FEFF, 변이 선택자) → 혼동문자 dict 치환 → 분리 자모 재조합(호환 자모를 첫가끝으로 매핑한 뒤 산술 조합) → 공백·구두점을 끼워 넣은 우회 패턴은 "공백 제거 버전"을 병행 검사한다. 원문과 정규화본을 둘 다 보존해, 차이가 크면 그 자체를 난독화 신호(특징값)로 쓴다.

---

## 주최 측 확인 질문 (Open Questions)

1. **Elice ML API 형식**: OpenAI Chat Completions 호환인가? base URL, 인증 헤더 형식, 모델 ID 목록(GPT·Claude 정확한 버전)은 무엇인가?
2. **구조화 출력**: `response_format={"type":"json_object"}` 또는 `json_schema`, tool/function calling을 지원하는가? Claude 모델도 같은 스키마로 호출되는가?
3. **임베딩**: 임베딩 엔드포인트가 있는가? 있다면 어떤 모델(OpenAI text-embedding-3, Solar 등)이고 차원과 단가는 얼마인가?
4. **한도·비용**: 팀별 크레딧 총량, 모델별 토큰 단가, RPM/TPM 제한, 동시성 제한, 타임아웃은 어떻게 되나? 평가 시 호출 비용은 누가 부담하나?
5. **로컬 모델 허용**: 평가 환경에서 HF 모델 가중치(약 0.6B KURE, 2.1B Kanana-Prompt)를 미리 넣어 두거나 런타임에 다운로드하는 것이 허용되는가? 인터넷 접근은 가능한가?
6. **패키지 제한**: 허용 목록이 있는가? 특히 `torch`, `transformers`, `sentence-transformers`, `onnxruntime`, `scikit-learn`, `openai` 각각의 허용 여부와 설치 용량 상한은?
7. **평가 하드웨어**: CPU 코어 수, RAM, 요청당 지연 상한(SLA)은?
8. **gpt-oss / HyperCLOVA X SEED 호스팅**: Elice가 호스팅하는 오픈 모델 중 `gpt-oss-safeguard-20b`나 가드 모델을 API로 쓸 수 있는가?
9. **필수 비교 대상**: "프레임워크 비교"에서 반드시 포함해야 하는 프레임워크가 지정되어 있는가(NeMo / Guardrails AI / LlamaFirewall)?
10. **데이터·PII 정책**: 테스트 데이터의 학번 형식(학교별 자릿수)은? 실제 PII를 외부 LLM(Elice 경유 GPT·Claude)으로 보내도 되는가, 아니면 반드시 사전 마스킹해야 하는가?

## 출처

- Elice ML API: https://elice.io/cloud/mlapi , 가격: https://elice.io/cloud/pricing , 모델 라이브러리 소개: https://brunch.co.kr/@elice/161
- Presidio 지원 엔티티: https://presidio.dataprivacystack.org/supported_entities/ , PyPI: https://pypi.org/project/presidio-analyzer/ , KR_RRN 관련 PR: https://github.com/openai/openai-guardrails-python/pull/29
- korean-pii: https://pypi.org/project/korean-pii/ , kpii: https://pypi.org/project/kpii/ , ko-pii-detector: https://huggingface.co/id4thomas/ko-pii-detector-ax-crf , MoAI: https://huggingface.co/BCCard/MoAI-Privacy-Filter-INT8
- Kanana Safeguard: https://huggingface.co/kakaocorp/kanana-safeguard-8b , https://huggingface.co/kakaocorp/kanana-safeguard-siren-8b , https://huggingface.co/kakaocorp/kanana-safeguard-prompt-2.1b , 발표: https://www.kakaocorp.com/page/detail/11568 , any-guardrail: https://docs.mozilla.ai/any-guardrail/api-reference/index/content-safety/kanana-safeguard
- Llama Prompt Guard 2: https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M , Llama Guard 3 1B: https://huggingface.co/meta-llama/Llama-Guard-3-1B
- Qwen3Guard: https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B , 기술 보고서: https://arxiv.org/abs/2510.14276
- ShieldGemma: https://huggingface.co/google/shieldgemma-2b , gpt-oss-safeguard: https://huggingface.co/openai/gpt-oss-safeguard-20b
- KURE-v1: https://huggingface.co/nlpai-lab/KURE-v1 , bge-m3: https://huggingface.co/BAAI/bge-m3 , ko-sroberta: https://huggingface.co/jhgan/ko-sroberta-multitask , Solar 임베딩 가격(제3자): https://pricingsaas.com/companies/upstage.api/diffs/2026W31
- MiniCheck: https://github.com/Liyan06/MiniCheck , Bespoke-MiniCheck-7B: https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B , HHEM: https://huggingface.co/vectara/hallucination_evaluation_model , LettuceDetect: https://github.com/KRLabsOrg/LettuceDetect , AlignRuScore: https://arxiv.org/abs/2512.06586 , RAGAS: https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/ , https://pypi.org/project/ragas/
- NeMo Guardrails: https://github.com/NVIDIA-NeMo/Guardrails , https://pypi.org/project/nemoguardrails/ , 모델 설정: https://docs.nvidia.com/nemo/guardrails/latest/configure-guardrails/yaml-schema/model-configuration
- Guardrails AI: https://pypi.org/project/guardrails-ai/ , 0.11 마이그레이션: https://guardrailsai.com/guardrails/docs/migration-guides/0-11-migration
- LlamaFirewall: https://pypi.org/project/llamafirewall/
- garak: https://pypi.org/project/garak/ , 번역: https://reference.garak.ai/en/latest/translation.html , OpenAI 제너레이터: https://reference.garak.ai/en/latest/generators/openai.html
- PyRIT: https://pypi.org/project/pyrit/ , 설정: https://microsoft.github.io/PyRIT/latest/getting-started/configuration , DeepTeam: https://pypi.org/project/deepteam/ , promptfoo: https://www.promptfoo.dev/docs/providers/openai/
- MAPIE: https://github.com/scikit-learn-contrib/MAPIE , https://pypi.org/project/mapie/ , crepes: https://github.com/henrikbostrom/crepes
- confusable_homoglyphs: https://github.com/vhf/confusable_homoglyphs , jamo: https://pypi.org/project/jamo/ , hangul-utils: https://pypi.org/project/hangul-utils/ , soynlp: https://pypi.org/project/soynlp/
