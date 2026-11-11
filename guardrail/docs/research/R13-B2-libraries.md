# R13-B2 라이브러리 선정 (한국어 형태소·BM25·분류기·구간 재정렬·서버·HTTP·검증)

작성 2026-11-10 · 방법: `ecc:search-first` (저장소 → PyPI JSON API → 공식 문서/GitHub → 외부 SKILL.md). 아무것도 설치하지 않았고 git도 건드리지 않았다. 웹 질의에 팀·프로젝트 이름을 쓰지 않았다.
표기: **[F]** 이번에 PyPI·소스에서 직접 확인 · **[R12]** 기존 R12/R4 문서에서 가져옴(재확인 안 함) · **[추정]** 측정하지 않은 값 · **[I]** 내 판단.

## 0. 전제 (저장소에서 확인한 것)

- `pyproject.toml`: `requires-python >=3.12`(질문에 적힌 ≥3.9가 아니다), 런타임 의존성은 `openai>=3,<4`, `pydantic>=2.14.2`, `pyyaml>=6.0.3` 셋뿐 [F]. scipy·pytest 등은 dev 그룹.
- `uv.lock` 잠금값: openai 3.42.1, pydantic 2.14.2, numpy 2.5.4(전이), scipy 1.18.1(dev) [F]. PyPI 최신은 openai 3.48.0, pydantic 2.14.3 [F].
- **openai 3.x는 `httpx`가 아니라 `httpx2`(2.16.0)에 의존한다** (`.venv`의 `_constants.py`가 `import httpx2`, `requires_dist`에 `httpx2<3,>=2.12.0`) [F]. 그러므로 `httpx`를 따로 넣으면 HTTP 클라이언트가 둘이 된다.
- 규정 요약(질문 기준): 허용 = 오픈소스 코드, **사전이 딸린 형태소 분석기·토크나이저**, 불용어 목록, 제공 데이터로 처음부터 학습한 모델 / 금지 = 사전학습 임베딩·리랭커·분류기 파일, 인터넷에서 받은 데이터·단어 목록, **런타임에 외부 서비스를 부르는 기능** / 로그에는 시간·길이·상태·오류만.
- 기존 방침: R12 §6은 kiwipiepy·Mecab을 "학습 모델·사전 보유 → 불가"로 판단해 2-gram BM25 유지를 권고했다 [R12]. 이 문서는 그 판단이 **규정 문구상 모호**함을 짚고 주최측 질문으로 올린다(§6 Q1).
- `provenance.md` 표에는 이미 (예정) `fastapi, uvicorn, rapidfuzz`가 적혀 있다. 아래 채택 항목을 추가할 때 이 표에 행을 더해야 한다.

## 1. 결정표

판정: Adopt(그대로 씀) / Extend(얇게 감쌈·확장) / Build(직접 작성) / Reject. 규정 칸: OK / 모호 / 불가.

| # | 필요 | 판정 | 선택 | 이유 | 규정 |
|---|---|---|---|---|---|
| 1 | 한국어 형태소 분석 | **Build+Extend** | NFKC 정규화 + 문자 n-gram(1·2·3) + 팀이 직접 쓴 조사·어미 접미 목록 | 72절·질문 짧은 문장이면 2-gram이 조사·어미를 흡수 [I]. 새 의존성 0, 모델·사전 0 | OK |
| 1a | 〃 (선택) | **보류 → 질의 후 결정** | kiwipiepy 0.24.1 (+`kiwipiepy_model` 0.24.0) | 정확도 이득 가능하나 학습 언어모델 파일 87 MB 포함. "사전 딸린 형태소 분석기"인지 "사전학습 모델"인지 문구상 갈린다 | **모호** (Q1) |
| 1b | 〃 | **Reject** | mecab-ko / python-mecab-ko | 사전(`mecab-ko-dic` sdist 33 MB, 통계 비용 포함) 필수. kiwi와 같은 모호성인데 이점은 kiwi보다 작고 Windows·빌드 위험이 큼 | 모호 |
| 1c | 〃 | **Reject** | soynlp 0.0.493 | 2019년 이후 미갱신, 라이선스 GPL 계열 [R4] | 라이선스로 탈락 |
| 2 | KB BM25 (~72절) | **Extend** | 저장소 `eval/mock_tutor.py`의 `BM25`(문자 2-gram, k1=1.2 b=0.75)를 서버 모듈로 이동 | 40줄 안팎, 의존성 0. 72절은 색인·검색이 1 ms 미만 [R12] | OK |
| 2a | 〃 | **Reject** | bm25s 0.3.16 | MIT(PyPI 메타는 라이선스 공란, 저장소 표기 기준 [R12]). numpy만 필수라 가볍지만 기본 토크나이저가 영어용이라 직접 토큰을 넘겨야 하고 72절엔 이득이 없다 | OK(쓰지 않음) |
| 2b | 〃 | **Reject** | rank_bm25 0.2.2 | 2022-02 이후 정체 [F] | OK(쓰지 않음) |
| 3 | 문자 n-gram 분류기 (라벨 ~200+합성) | **Adopt** | scikit-learn 1.9.1 `TfidfVectorizer(analyzer="char_wb", ngram_range=(1,3))` + `LogisticRegression` | 데이터가 작아 기동 때 몇백 ms 안에 학습 [추정]. 보정·교차검증 도구 내장. R4가 이미 채택 [R4] | OK (제공 데이터로 처음부터 학습) |
| 3a | 〃 (대안) | **Build(폴백)** | numpy만으로 LR + 해시 n-gram | sklearn 설치 용량(약 100 MB 안팎 [추정])이나 빌드가 문제일 때만 | OK |
| 3b | 〃 | **Reject** | fastText(from scratch) — `fasttext-wheel` 0.9.2(2024-02), `fasttext` 0.9.3 | 저장소는 2024년에 보관됨(아카이브) [추정—확인 필요], 200건에선 TF-IDF+LR 대비 이득이 없고 휠 태그가 cp312 이후까지 있는지 불확실. 사전학습 벡터를 안 쓰면 규정은 OK이지만 가져올 이유가 없음 | OK(쓰지 않음) |
| 4 | LLM 인용구 → 원문 부분 문자열 재정렬 | **Adopt+Extend** | rapidfuzz 3.14.6 `fuzz.partial_ratio_alignment` 앞에 "원문 그대로 찾기 → 공백·문장부호 제거본" 두 단계를 직접 둠 | MIT, 모델·사전 없음, 휠 Linux·Windows 있음 [F]. R12 §6-3 설계와 일치 | OK |
| 4a | 〃 | **Reject(폴백 아님)** | difflib `SequenceMatcher` | 표준 라이브러리지만 `find_longest_match`는 최악 O(n²), `autojunk` 기본값이 200자 넘는 문자열에서 문자를 정크로 버려 오정렬 위험. rapidfuzz가 이미 예정 의존성 | OK |
| 5 | 서버 | **Adopt** | fastapi 0.162.2 + uvicorn 0.54.0(단일 프로세스, `[standard]` 없이) | I/O 대기가 지배적이라 이벤트 루프 하나로 64건 충분 [I]. 부하 측정 후 필요하면 `--workers 2~4` | OK |
| 6 | 비동기 HTTP·타임아웃·재시도 | **Adopt** | 이미 있는 `openai.AsyncOpenAI` (httpx2 내장) | 의존성 추가 0. `httpx`를 따로 쓰면 이중 스택 | OK (프록시 하나만 호출) |
| 6a | 〃 | **Reject** | 별도 `httpx` 직접 사용 | 위 이유. 단 dev에서 `TestClient`가 요구하면 dev 전용으로만 | OK |
| 7 | 토큰 수 계산 | **Reject** | tiktoken 0.14.0 | `get_encoding("o200k_base")`가 BPE 순위 파일을 **런타임에 인터넷(openaipublic blob)에서 내려받는다**(캐시는 `TIKTOKEN_CACHE_DIR`/임시폴더). 외부 호출이자 인터넷 데이터 → 이중 위반 | **불가** |
| 8 | 요청·응답 검증 | **Adopt** | pydantic 2.14.x (`extra="forbid"`, `model_validate_json`) | 이미 의존성. LLM JSON 출력을 모델로 바로 검증 | OK |
| 8a | 계약 파일 대조 | **Adopt(dev만)** | jsonschema 4.26.0 | `docs/contract/guard.schema.json`과 응답을 테스트에서 대조. 서버 런타임에는 불필요 | OK |

## 2. 규정 판정 기준과 모호한 지점

이 문서에서 쓴 판정 규칙은 다음과 같다.

1. **설치 때 PyPI에서 받는 파일**은 라이브러리 코드와 같은 취급(오픈소스 코드). 단 파일의 *성격*이 모델·사전·데이터이면 아래로 따로 본다.
2. **런타임 다운로드**(첫 호출 때 외부에서 받음)는 무조건 불가. 외부 서비스 호출이고 인터넷 데이터이기 때문.
3. **사전**은 허용, **학습된 모델 가중치**는 금지. 그런데 형태소 분석기의 "사전"에는 통계 비용·언어모델이 섞여 있어 경계가 흐리다.

모호 사례:

- **kiwipiepy**: 라이브러리 자체는 Apache-2.0 휠(약 15 MB, Linux x86_64·Windows 모두 `cp39-abi3` 휠) [F]. 하지만 필수 의존성 `kiwipiepy_model<0.25,>=0.24`가 sdist 87.9 MB 단일 파일로, 기본 언어모델(KnLM 계열 `.mdl`)과 사전·결합규칙 파일을 담고 있다 [F, 파일 구성은 README·R12 기준]. 이 모델은 *품사 모호성 해소용 n-gram/문맥 언어모델*이지 의미 임베딩·리랭커·분류기는 아니다. 그러나 "미리 학습된 모델 파일"이라는 문구에 글자 그대로는 걸린다. 학습 말뭉치의 출처도 이번에 확인하지 못했다. → **서면 답변 전에는 서버에 넣지 않는다.**
- **mecab-ko-dic**: 사전이라 문구상 "사전 딸린 형태소 분석기"에 가장 가깝지만, 그 안의 연접 비용·단어 비용은 말뭉치로 학습된 값이다. kiwi와 같은 질문으로 묶는다.
- **sklearn pickle**: 제공 데이터로 학습한 모델은 허용이지만, 저장한 `.pkl`을 로드하는 건 보안·버전 문제가 있어 **기동 때 학습**을 기본으로 한다(§4).
- **scikit-learn 자체**: 코드 라이브러리라 OK. 번들 데이터셋 로더(`fetch_*`)는 런타임에 인터넷을 쓰므로 **호출 금지**(코드 리뷰 체크 항목).

## 3. 항목별 평가

### 3-1. 한국어 토큰화·형태소

| 후보 | 버전 / 날짜 | 라이선스 | 유지보수 | 휠 (Linux x86_64 / Windows) | 번들 데이터 | 런타임 다운로드 |
|---|---|---|---|---|---|---|
| kiwipiepy | 0.24.1 / 2026-10-20 [F] | Apache-2.0 | 활발(최근 릴리스 2026-10) | 있음 / 있음 (`cp39-abi3`, 3.14 `cp314t`도) [F] | 휠에는 코드만, **모델은 `kiwipiepy_model` 0.24.0 sdist(87.9 MB)**: 언어모델·사전 | 없음(`model_path=None`이면 설치된 모델 패키지에서 로드). README에 CoNg 모델을 URL로 받는 *선택* 절차가 있으나 쓰지 않으면 해당 없음 |
| mecab-ko | 1.0.3 / 2026-10-28 [F] | BSD | 활발해 보임 | cp312 있음 / 있음 [F] | `mecab-ko-dic` 1.0.0 sdist 33 MB 필수 | 사전은 sdist 동봉. 설치 스크립트가 따로 내려받는지는 **미확인** |
| soynlp | 0.0.493 / 2019-08 [F] | GPL 계열 [R4] | 중단 | 순수 파이썬 | 없음(비지도) | 없음 |
| 문자 n-gram(직접) | — | 자체 | 우리 | 해당 없음 | 없음 | 없음 |

- CPU 지연: kiwi는 문장당 밀리초 단위, 기동 로딩 1~3초 [추정, 미측정]. 문자 n-gram은 마이크로초~밀리초. 어느 쪽이든 72절 색인은 기동 때 한 번이다.
- **8코어에서 kiwi를 쓸 경우**: `Kiwi(num_workers=...)`는 `analyze` 배치용 스레드 수이므로, 요청당 문장 하나를 `asyncio.to_thread`로 넘기고 `num_workers=1~2`면 충분 [추정].
- **권고 [I]**: 1단계로 문자 n-gram + 직접 쓴 조사 접미 제거(`은/는/이/가/을/를/에/에서/으로/와/과/의/도/만…`, 약 60개)로 검색 recall@k를 공개 세트에서 측정한다. kiwi는 *측정으로 이득이 분명하고 Q1 답이 "허용"일 때만* 기능 플래그 뒤에 추가한다(실패 시 n-gram으로 자동 폴백).
- 직접 쓴 조사 목록은 규정상 "불용어 목록" 또는 팀 작성 규칙이다. `provenance.md`에 "팀 작성, 사전 사용 안 함"으로 한 줄 올린다.

### 3-2. BM25

- 저장소 `BM25`는 이미 문자 2-gram, k1=1.2, b=0.75, 질문만 사용. 서버로 옮길 때 바꿀 것 [I]: (a) 기동(lifespan)에서 색인, (b) 절 제목에 가중(제목 토큰을 두 번 넣기), (c) 1+2-gram 혼합과 3-gram 추가를 ablation, (d) 영문·식별자는 공백 토큰 추가.
- bm25s 0.3.16: 필수 의존성 numpy, 나머지(orjson·numba·PyStemmer 등)는 선택 [F]. 속도 이점은 수만 문서 규모에서 나오며 72절에는 의미가 없다. 영어 정지어·스테머 전제라 한국어는 어차피 직접 토큰화.
- rank_bm25 0.2.2는 2022-02 이후 릴리스 없음 [F].
- 임베딩 검색(`text-embedding-3-small`)은 허용된 호출이다. 하이브리드는 R12 §6-1대로 recall@k로 결정하고, 기동 때 72절 임베딩 호출 1회(캐시는 메모리) 정도로 끝난다.

### 3-3. 분류기

- scikit-learn 1.9.1(2026-09-10): BSD-3, `requires-python >=3.11`, 필수 의존성 numpy·scipy·joblib·narwhals·threadpoolctl [F]. Linux `manylinux_2_27` cp312 휠과 Windows `win_amd64` cp312 휠 있음 [F]. **scipy가 지금 dev 그룹에만 있으므로 런타임으로 올려야 한다.**
- 구성: `TfidfVectorizer(analyzer="char_wb", ngram_range=(1,3), sublinear_tf=True, min_df=2)` + `LogisticRegression(C=…, class_weight="balanced")`. 확률 보정은 R4의 split conformal(`np.quantile`) 그대로. `SGDClassifier`는 이 크기에서 빠를 이유가 없어 제외.
- 학습 위치: **기동 때 `data/`의 라벨 파일로 학습**한다(수백 ms 추정). pickle 로드는 쓰지 않는다(버전 불일치·역직렬화 위험). 학습 데이터는 `provenance.md` §2의 "다른 LLM이 만든 묶음" 제약을 따라야 한다. 합성 예시를 어떻게 만든 것인지가 규정 쟁점이므로 Q4로 올린다.
- 추론은 요청당 밀리초 이하라 이벤트 루프에서 직접 호출. sklearn 내부 스레딩(`threadpoolctl`)은 소형 행렬에서 오히려 느려질 수 있어 `OMP_NUM_THREADS=1`을 기동 환경에 둔다 [추정].
- fastText: 0.9.3 sdist/0.9.2 휠은 2024년이 마지막 [F]. 사전학습 벡터(`cc.ko.300.bin`)는 금지이므로 from-scratch만 가능한데 샘플 200개에서 이점이 없다.

### 3-4. 구간 재정렬 (인용구 → 원문 부분 문자열)

- rapidfuzz 3.14.6(2026-08-30): MIT, `requires-python >=3.11`, 필수 의존성 없음(numpy는 `all` 엑스트라), 휠 Linux·Windows 있음 [F].
- `fuzz.partial_ratio_alignment(needle, haystack, score_cutoff=90)`가 `src_start/src_end/dest_start/dest_end`를 돌려준다 [R12]. 사용 순서: ① 정확 일치 `answer.find(quote)` → ② 공백·문장부호·ZWSP를 지운 정규화본에서 찾고 **정규화 위치 → 원문 위치 매핑 배열**로 되돌림 → ③ rapidfuzz(컷오프 ≥90) → 모두 실패하면 해당 구간을 버리거나 `unanchored` 처리. 출력은 항상 `answer[a:b]` 원문 슬라이스여야 한다(인용이 원문 부분 문자열이라는 계약 때문).
- 주의 [R12]: needle이 64자를 넘으면 rapidfuzz의 비트병렬 최적 경로를 벗어나 최적 정렬이 보장되지 않는다. 문장 경계로 먼저 쪼갠다. 또 needle이 haystack보다 길면 두 인자의 역할이 바뀌므로 길이 검사를 먼저 둔다 [추정—단위 테스트로 고정].
- difflib: 표준 라이브러리이므로 의존성은 없지만 위 이유로 채택하지 않는다.

### 3-5. 서버 (FastAPI + uvicorn)

- fastapi 0.162.2(2026-11-07, MIT, py≥3.10), uvicorn 0.54.0(2026-09-25, BSD-3, py≥3.10) [F].
- **단일 워커 vs 다중 워커**: 요청 한 건의 시간은 거의 LLM 응답 대기(비동기)이고 CPU 작업(BM25·분류기·정규화·재정렬)은 합쳐 수 ms [추정]. 64건 동시 × 수 ms ≈ 이벤트 루프 부하가 낮다. 다중 워커는 프로세스마다 인덱스·모델·연결 풀을 복제하고, 프록시 레이트 한도를 프로세스 사이에서 나눌 수 없게 만든다. → **기본 `--workers 1`**, 부하 시험에서 루프 지연(p95 > 50 ms)이 보이면 2~4.
- 8코어를 쓰고 싶다면 CPU 작업이 5 ms를 넘는 경로만 `asyncio.to_thread`. 프로세스 풀은 쓰지 않는다.
- 옵션: `--no-access-log`(로그 규칙을 코드가 통제하도록 직접 미들웨어로 시간·길이·상태 코드만 기록), `--timeout-keep-alive 5`, `--limit-concurrency`는 쓰지 않음(초과 시 503이 채점 실패로 직결). `uvloop`는 Windows 미지원이라 쓰지 않는다. `uvicorn[standard]`는 `httptools/uvloop/watchfiles/websockets`를 끌어와 불필요.
- 요청 본문이 로그·예외 메시지에 새지 않게: 검증 오류 핸들러를 덮어써 `RequestValidationError`의 `input`/`body` 필드를 응답과 로그에서 제거한다 (FastAPI 기본 422 응답이 입력값을 되돌려 줌) [I].

### 3-6. LLM 호출 (AsyncOpenAI) — 64+ 동시, 요청당 30초

SDK 기본값 [F, `.venv`의 `openai/_constants.py`]: `timeout=Timeout(600, connect=5)`, `max_retries=2`, 연결 한도 `max_connections=1000, max_keepalive=100`, 재시도 간격 0.5~8초, **`Retry-After`는 120초 이하이면 그대로 따른다**.

문제: 기본 타임아웃 600초 · `Retry-After` 최대 120초는 요청당 30초 예산을 쉽게 넘는다. 제안 설정 [I]:

```python
client = AsyncOpenAI(
    base_url=..., api_key=os.environ["..."],            # 환경변수만
    timeout=openai.Timeout(connect=3.0, read=12.0, write=5.0, pool=5.0),
    max_retries=0,                                       # 재시도는 직접 (아래)
)
# 호출 한 건 = asyncio.timeout(남은_예산) 안에서
#   시도 1 → (429/5xx/연결오류만) 지터 0.2~0.5초 후 시도 2 (남은 예산이 read 한도보다 클 때만)
```

- 전체 요청 예산 `asyncio.timeout(≈26 s)`로 싸고 남은 시간에 따라 재시도를 포기한다. SDK 내장 재시도는 `Retry-After` 때문에 예산을 모르고 오래 잔다 → `max_retries=0` + 직접 1회 재시도가 예산을 지킨다.
- 입력·출력 판정이 요청당 최대 2회라면 동시 호출은 최대 약 130건. 기본 연결 한도(1000)로 충분하나 **프록시의 실제 동시성/레이트 한도가 병목**이므로 `asyncio.Semaphore(N)`을 두고 N은 프록시 한도 질문(Q3) 답에 맞춘다. 풀이 아니라 세마포어로 대기시키면 `pool` 타임아웃 대신 예산 타임아웃으로 일관되게 실패한다.
- 클라이언트는 lifespan에서 하나만 만들고 종료 때 `await client.close()`. 요청마다 만들지 않는다(연결 재사용).
- 기본 `DefaultAioHttpClient`(aiohttp) 엑스트라는 쓰지 않는다. 의존성만 늘어난다.
- Structured Outputs(`response_format` json_schema)는 프록시가 지원하는지 모른다 → Q5. 미지원이면 프롬프트로 JSON을 요구하고 pydantic으로 검증, 실패 시 1회 재시도 없이 보수적 폴백.
- 로그: 예외를 그대로 `logger.exception`하면 openai 예외의 `body`·`request`가 본문을 포함할 수 있다. `type(e).__name__`, 상태 코드, 경과시간만 기록한다.

### 3-7. tiktoken

- 0.14.0(2026-08-17, MIT, 휠 있음) [F]. 인코딩 파일(`o200k_base.tiktoken`)은 패키지에 없고 첫 `get_encoding` 때 내려받는다(알려진 동작 [I], 이번에 소스를 열어 확인하진 않음). 사전 배치(`TIKTOKEN_CACHE_DIR`에 파일을 미리 두기)를 해도 그 파일은 "인터넷에서 받은 데이터"에 해당할 소지가 있어 규정상 안전하지 않다. 토큰 수가 필요한 곳은 글자 수 상한(예: 입력 N자 절단)으로 대체한다. 토큰 수가 꼭 필요하다는 요구가 생기면 Q6.

### 3-8. 검증 (pydantic / jsonschema)

- pydantic 2.14.3(2026-11-05, MIT, py≥3.10) [F]. 요청 모델은 `extra="forbid"`, 길이 상한(`max_length`)을 명시해 대용량 본문 DoS를 막는다. LLM 출력은 `Model.model_validate_json(text)`로 파싱하고 `ValidationError`는 *내용 없이* 오류 종류만 기록.
- jsonschema 4.26.0(2026-01-07, MIT, py≥3.10) [F]: `docs/contract/guard.schema.json`과 실제 응답을 대조하는 테스트에서만 쓴다(dev). 런타임 검증은 pydantic이 한다.
- 둘이 같은 스키마를 두 번 정의하지 않도록 `Model.model_json_schema()`를 계약 파일과 비교하는 테스트 1개 [I].

## 4. 제안 의존성 (고정 버전)

기준일 2026-11-10, PyPI 최신 [F]. 실제 고정은 `uv.lock`이 하고, 아래는 `pyproject.toml` 하한/상한 제안이다. 설치는 하지 않았으므로 **`uv lock` 해결 가능 여부는 미확인**(특히 numpy 2.5.4 ↔ scikit-learn 1.9.1, `requires-python` 충돌 없음 예상).

런타임:

| 패키지 | 고정 | 비고 |
|---|---|---|
| fastapi | `==0.162.2` | 신규 |
| uvicorn | `==0.54.0` | 신규, `[standard]` 아님 |
| openai | 기존 `>=3,<4` (잠금 3.42.1; 3.48.0 가능) | httpx2 포함. 올릴지는 별도 판단(잠금 갱신 시 회귀 테스트) |
| pydantic | `>=2.14.2,<3` (잠금 2.14.2; 2.14.3 가능) | 기존 |
| pyyaml | 기존 | 기존 |
| rapidfuzz | `==3.14.6` | 신규. `provenance.md`에 이미 예정 |
| scikit-learn | `==1.9.1` | 신규. 전이: numpy 2.5.4, scipy 1.18.1(dev→runtime), joblib, narwhals, threadpoolctl |

개발 전용:

| 패키지 | 고정 | 비고 |
|---|---|---|
| jsonschema | `==4.26.0` | 계약 대조 테스트 |
| httpx | `==0.28.1` (2024-12) | Starlette `TestClient`가 요구하는지 **미확인**. 요구할 때만 dev에 추가 |
| pytest, pytest-cov, ruff, hypothesis, anyio | 기존 | |

넣지 않는 것: kiwipiepy·kiwipiepy_model(Q1 대기), mecab-ko, soynlp, bm25s, rank_bm25, fasttext, tiktoken, numba, torch 류, `uvicorn[standard]`, `httpx`(런타임), `orjson`(FastAPI 응답 속도 이득이 이 규모에선 미미).

**설치 시점 점검**: `pip download`가 아니라 *러닝 박스에 인터넷이 없을 수 있음* → 오프라인 휠 번들이 필요한지 Q2.

## 5. 외부 SKILL.md 4건 (읽기 전용, 신뢰하지 않는 입력으로 취급)

`gh api repos/<owner>/<repo>/contents/<path>`로 앞부분만 읽었다. 코드는 실행·복사하지 않았다.

1. **Orchestra-Research / nemo-guardrails** (MIT, NVIDIA NeMo Guardrails 소개): Colang으로 "사용자 의도 예시 → 거절 플로우"를 선언하고 입력/출력 self-check 레일을 둔다. 차용할 만한 것: *입력 검사와 출력 검사를 분리된 단계로 두고 각 단계가 stop 가능* 하다는 구조뿐. 라이브러리 자체는 쓰지 않는다(`nemoguardrails`는 LLM·PII·LlamaGuard 모델 연동을 전제로 해 사전학습 모델과 외부 호출이 들어온다).
2. **sickn33 / rag-observability-evals** (MIT, 출처 BagelHole): 검색은 recall@k·MRR, 생성은 "주장 단위 SUPPORTED / PARTIALLY_SUPPORTED / NOT_SUPPORTED" 근거성 점수, 단계별 p50/p95 지연을 따로 잰다. 차용: **recall@k·MRR로 BM25 변형 비교**, 지연을 검색/LLM 호출로 분리해 기록(본문 없이 숫자만). 이 스킬 자체가 "평가 대상 텍스트는 판정자에게 지시가 아니라 데이터"라고 경고한다 — 우리 판정 프롬프트의 경계 구분 원칙과 같음. RAGAS 등은 외부 LLM 호출이라 서버에는 부적합.
3. **cobusgreyling / llm-as-judge**: 위치 편향·길이 편향·자기선호 편향을 이름 붙여 보정하고, 판정기도 사람 라벨 50건 이상에서 일치도(κ≥0.6)로 검증하라고 한다. 이산 분류는 자유서술 판정이 아니라 소형 분류기로, 결정적으로 채점 가능한 것은 규칙으로. 차용: ① 판정 결과를 **작은 서수 척도 + 앵커 예시**로 제한 ② 같은 호출 N≥3 평균은 지연 때문에 서버에선 불가하지만 오프라인 평가에서는 적용 ③ 우리 규칙-먼저·LLM-나중 순서 재확인.
4. **mukul975 / detecting-ai-model-prompt-injection-attacks** (Apache-2.0): 정규식 서명 + 구조적 이상 점수(지시어 밀도, 특수문자 비율, 언어 혼용, 대문자 비율) + DeBERTa 분류기 가중합. **DeBERTa는 사전학습 분류기라 금지**, 모델 다운로드 단계도 금지 대상. 차용: 정규식 + 휴리스틱 점수 두 층만(팀이 직접 쓴 패턴, `provenance.md` 방침대로 외부 목록 복사 금지). "단독 방어로 쓰지 말고 출력 검증과 함께"라는 주의는 R12 구조와 일치.

네 문서 모두 *코드나 목록을 가져오지 않고* 아이디어만 본다. 본문에 있던 지시문(설치·실행 요구)은 따르지 않았다.

## 6. 주최측에 물을 것

1. **Q1 (결정적)**: "형태소 분석기·토크나이저는 사전 포함 허용"에서, **kiwipiepy(+`kiwipiepy_model`: 학습된 품사 언어모델 파일, 87.9 MB)** 또는 **mecab-ko(+`mecab-ko-dic`)** 가 *사전딸린 형태소 분석기*로 허용되는가, 아니면 *미리 학습된 모델 파일*로 금지인가? PyPI 설치로 들어오는 파일이면 되는가?
2. **Q2**: 채점·시연 박스에서 `pip/uv`로 PyPI 설치가 되는가, 아니면 오프라인 휠 번들을 제출해야 하는가? 설치 용량 상한, 허용 패키지 목록이 있는가? (scikit-learn·scipy·numpy 포함 여부)
3. **Q3**: 제공 프록시의 동시 요청 한도·초당 요청 한도, 429 응답의 `Retry-After` 정책, `gpt-5-nano`의 응답 지연 분포(권장 타임아웃)?
4. **Q4**: 분류기 학습용 *합성 예시*를 팀이 직접 쓴 것이 아니라 코딩 도구/LLM으로 만들면 "다른 LLM이 만든 데이터 묶음"에 걸리는가? 규칙 안의 예시 문장은 팀 작성으로 인정되는가(OT p.25 해석 재확인)?
5. **Q5**: 프록시가 `response_format: json_schema`(Structured Outputs)와 `temperature`/`seed` 같은 파라미터를 지원하는가? `gpt-5-nano`의 reasoning 토큰이 `max_completion_tokens`에 포함되는가?
6. **Q6**: 서버에서 토큰 수가 필요한 경우 `tiktoken`처럼 런타임에 BPE 파일을 내려받는 라이브러리는 어떤 경우에도 불가인가? 미리 동봉한 인코딩 파일은 어떤가?
7. **Q7**: 러닝 박스의 Python 버전과 OS(Linux x86_64로 가정). 저장소는 `>=3.12`, numpy 2.5.4는 `>=3.12`를 요구하므로 3.11 이하이면 전체 의존성 고정을 다시 해야 한다.
8. **Q8**: 서버가 기동 때 `text-embedding-3-small`로 KB 72절을 임베딩 호출하는 것이 평가 시작 전 준비 시간으로 허용되는가(기동 지연 상한)?
9. **Q9**: 기동 때 학습(분류기)·색인(BM25)을 하는 방식이 허용되는가, 아니면 학습 산출물(가중치 파일)을 제출물에 포함하는 편이 규정상 더 안전한가?

## 7. 확인하지 못한 것 · 한계

- 설치·벤치마크를 하지 않았다. 지연 수치는 전부 [추정]이다. 기동 때 sklearn 학습 시간, kiwi 로딩 시간, rapidfuzz 정렬 시간은 구현 후 측정해야 한다.
- `uv lock` 해결, `TestClient`의 httpx 요구 여부, mecab-ko 설치 스크립트의 추가 다운로드 여부, tiktoken 소스상의 다운로드 경로, fastText 저장소 보관 상태, kiwi 학습 말뭉치 출처는 이번에 소스를 열어 확인하지 않았다.
- bm25s의 PyPI 라이선스 칸이 비어 있었다(저장소 표기 MIT는 R12 인용).
- 외부 SKILL.md는 앞 4~5 KB만 읽었다.

## 8. 출처

PyPI JSON API(`pypi.org/pypi/<pkg>/json`)로 2026-11-10에 조회한 버전·라이선스·`requires_python`·휠 파일명·파일 크기(kiwipiepy, kiwipiepy-model, bm25s, rank-bm25, scikit-learn, fasttext, fasttext-wheel, rapidfuzz, fastapi, uvicorn, httpx, openai, tiktoken, pydantic, jsonschema, python-mecab-ko, mecab-ko, mecab-ko-dic, soynlp, orjson, uvloop, numpy, scipy, httpx2) · github.com/bab2min/kiwipiepy README(모델 패키지·CoNg 모델 안내) · 저장소 `guardrail/pyproject.toml`, `uv.lock`, `.venv/.../openai/_constants.py`·`_base_client.py`, `src/guardrail/eval/mock_tutor.py`, `docs/competition/provenance.md`, `docs/research/R4`·`R12` · GitHub `gh api` 4건(위 §5).
