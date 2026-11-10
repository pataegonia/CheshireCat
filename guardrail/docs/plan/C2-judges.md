# C2 판정 v1 — 세부 플랜

작성 2026-11-10 · 담당 danielcho02 · 상태: v1 (master-plan v23 기준)
상위: [master-plan](../master-plan.md) · 요구사항: [PRD v6](../PRD.md) · 결정: [R13 D1–D10](../research/R13-design-research.md#3-결정-d1d10) · 기간: 11/12(목) 오후–11/15(일)

## 1. 목표와 관문

**보이는 변화**: PRD §4의 ①–⑨가 모두 붙은 `Guard.judge()`가 서버와 하네스에서 돈다. 하네스가 짝 묶음 5-분할 교차 검증으로 가드 점수, block F1, 출력 평균을 3회씩 내고, nano 단독 베이스라인·kNN 단독·LR 단독과 비교한다. v1을 제출한다.

**관문**: ① 교차 검증 가드 점수가 nano 단독 베이스라인보다 높고 3회 범위가 겹치지 않음(PRD §7) ② 동시 64·약 600요청에서 시간 초과 0(GR-10) ③ santa 통과한 프롬프트 2개 ④ CI green.

공통 계약은 [master-plan §4](../master-plan.md#4-공통-계약). 각 슬라이스는 자기 모듈과 테스트만 만지고, `config.py`·`default.yaml`에는 자기 키만 더한다.

## 2. 날짜표

| 날짜 | 본인(Opus) | 병렬(Sonnet, worktree) | 끝나면 |
|---|---|---|---|
| 11/12 목 오후 | S2.1 머지 뒤 S2.3 신호 시작 | S2.1 전처리 → S2.2 KB · S2.5 kNN·LR | S2.1 PR |
| 11/13 금 | S2.6 입력 판정(프롬프트 → santa 병렬) | S2.2·S2.5 마무리 → S2.9 하네스 골격(`--fake`) | S2.2·S2.3·S2.5 PR |
| 11/14 토 | S2.7 출력 판정(santa 병렬) → S2.4 규칙 | S2.9 계속 | S2.6·S2.7 PR |
| 11/15 일 | S2.8 조립 → S2.9 첫 실측 → S2.10 부하·v1 제출 → Phase 끝 점검 | S2.11(P1) | 관문 판정 |

합계 50h(본인 약 32h + 병렬 18h). 11/15 밤 관문을 못 넘으면 [master-plan §7](../master-plan.md#7-자르는-순서-일정이-밀릴-때) 순서로 자르고 11/16을 쓴다. S2.10의 v1 제출은 완충으로 11/16 오전까지 밀릴 수 있다.

## 3. 슬라이스

### S2.1 전처리 v6 (숨긴 문자 드러내기)

| GR / ADR | GR-02 · [ADR-0001](../adr/0001-korean-normalization-skeleton-view.md) | 우선·예상·모델 | P0 · 3h · Sonnet |
|---|---|---|---|
| 선행 | S1.1(`Message`·`Signals` 타입은 S1.1이 만든다) | 브랜치 | `gr/s2.1-preprocess` |

- **바꿀 파일**: `src/guardrail/preprocess.py`, `tests/test_preprocess.py`.
- **정규화본** `normalize(text)`: 지금 그대로(보이지 않는 문자 제거 → 전각·반각 접기 → 자모 조합). **NFKC는 쓰지 않는다**(x² → x2처럼 코드·수식 뜻이 바뀜). 기존 회귀 테스트를 유지한다.
- **붙인 뷰** `joined(norm)`는 **구현하지 않는다**(기본 신호가 읽지 않고 토글도 없다. 필요하면 ablation으로 추가). 정의(추가할 때 쓸 것): 한 음절짜리 한글 토큰이 공백 하나를 사이에 두고 2개 이상 이어진 구간의 공백만 지운다("광 고" → "광고", "발 급 수 수 료" → "발급수수료"). "자 살펴볼까요"처럼 일반 띄어쓰기는 붙이지 않는다. **어떤 기본 신호도 이 뷰를 읽지 않는다.** 거절 정규식에 걸면 "알려 줄 수 없어요"의 "줄 수"가 붙어 튜터 거절 36건 중 3건을 놓친다([R13-A §6](../research/R13-public-set-analysis.md#6-규칙-②-오탐-재측정-santa-4라운드)). 신호별 뷰: 거절 정규식 = 원문에서 인용·코드 뺀 것, 숫자·리터럴 = 원문, 저신호 = 조합 전 텍스트, LLM·kNN·LR = 정규화본.
- `skeleton`과 그 테스트는 지운다(v6 서버 경로에서 쓰지 않음).
- `reveal_hidden(answer)`: 태그 문자 U+E0020–E007E → 대응 ASCII, 나머지 폭 없는 문자 → `<U+200B>` 형식. 이모지 시퀀스 안의 ZWJ·변형 선택자, 깃발 태그(S2.3 정의)와 맨 앞 BOM은 그대로. 원문은 따로 보존한다(구간은 원문 기준).
- `decode_tags(text) -> tuple[str, str]`: 학습자 발화의 태그 문자를 풀어 `(태그를 지운 정규화본 + " [숨긴 문자: …]", 푼 문자열)`.
- `strip_tags(text) -> str`: 태그 문자 U+E0000–E007F만 지운다(자모 조합·다른 정규화 없음). S2.3 `is_low_signal`의 입력 뷰다.
- **테스트 먼저**: 태그·ZWSP 드러내기, 이모지 ZWJ 보존, `hypothesis` 임의 유니코드 예외 0, `strip_tags`가 태그 문자만 지우고 "ㅋㅋㅠㅠ"를 조합하지 않음, 400행 p95 ≤10ms. GR-02의 KB 코드 블록 오매칭 0은 신호 정규식이 생기는 S2.3에서 잰다.
- **수용 기준**: GR-02 수용 기준 중 KB 코드 블록 오매칭(S2.3)을 뺀 전부. **스킬**: `ecc:python-testing`.

### S2.2 KB 색인·검색

| GR / ADR | GR-03 · [ADR-0010](../adr/0010-guard-side-kb-retrieval.md) | 우선·예상·모델 | P0 · 4h · Sonnet |
|---|---|---|---|
| 선행 | S1.4, S2.1 | 브랜치 | `gr/s2.2-kb` |

- **만들 파일**: `src/guardrail/kb.py`, `tests/test_kb.py`. BM25는 R13-A가 잰 변형인 `guardrail/scripts/r13/common.py`의 BM25(문자 2-gram, k1=1.2, b=0.75)를 옮긴다. 토큰화는 `scoring.normalize`식(NFKC, 소문자, 공백·문장부호 제거 뒤 문자 2-gram)이다. 이 BM25는 질의 2-gram의 빈도를 무시한다(`scores()`가 질의의 서로 다른 2-gram에 대해서만 합산하고 `Counter`의 개수는 쓰지 않는다). `rule_min_margin`이 절대 점수에 걸리므로 이 성질까지 그대로 옮기고, 일반 BM25의 질의 빈도 가중을 넣지 않는다. 조각은 `common.kb_chunks`를 그대로 옮긴다. 섹션 텍스트(md + 붙은 CSV)를 `## `로 시작하는 줄 앞에서 자른다(`re.split(r"\n(?=## )")`). 코드 블록 안의 `## ` 줄에서도 자른다(PR-02-01에 3곳). 각 조각은 자기 `## ` 소제목 줄을 포함하고, 첫 `## ` 앞의 `# 제목` 머리말은 따로 한 조각이 된다. 조각마다 문서 제목을 덧붙이지 않는다. CSV는 섹션 끝에 붙어 마지막 조각에 들어간다. 코드 블록을 자르지 않는 변형은 다시 재야 하는 ablation이다. `mock_tutor.py`의 변형은 쓰지 않는다(ADR-0010 v6). 아래 목표(1위 ≥64/67, 3위 ≥66/67, 규칙 기본 V2의 U 18/35·G 0/32·T 6/119)는 이 변형의 값이라, 구현이 달라지면 다시 잰다. 이 NFKC는 BM25 토큰 안에서만 쓰고 정규화본에는 쓰지 않는다(ADR-0001).
- **계약**: `KbIndex.load(kb_dir) -> KbIndex`, `search(query: str, k=3) -> list[Hit]`, `margin(query: str) -> float`(top_k 안만이 아니라 **전체 순위**를 섹션별 최고 점수로 접은 뒤 1위 섹션 점수 − 그와 **다른** 섹션 중 최고 점수), `section_text(section_id) -> str`(그 섹션의 모든 조각 + 붙은 CSV, 규칙 ②의 비교 대상). `Hit(section_id, text, score)`.
- 조각: `kb/<과목>/<섹션 ID>.md`에 같은 이름의 `-table<n>.csv`를 붙인 섹션 텍스트를 위 `kb_chunks` 규칙대로 자른다. HTML은 읽지 않는다. 조각 id = 섹션 ID(소제목 순번은 내부용).
- 질의 문자열을 만드는 함수는 `guard.py` 몫이다(창 안의 학습자 발화 + 후보 답변). 측정한 질의는 모든 user 발화 + 답변(`all_user + "\n" + candidate_answer`)이다. 가드 공개 세트는 메시지가 5개 이하라 창(첫 학습자 발화 + 마지막 6개) 안의 학습자 발화와 같다. 튜터 공개 세트는 최대 11메시지(7개 초과 58행)라 창과 다르지만, 창 질의로 q6를 다시 돌려도(`q6_rule_fp.py --window`) U·G·T 수치와 F1이 같았다([R13-A §6](../research/R13-public-set-analysis.md#6-규칙-②-오탐-재측정-santa-4라운드)). 여기서는 문자열만 받는다.
- **테스트 먼저**: 조각이 `common.kb_chunks`와 같음(PR-02-01 코드 블록 안 `## ` 3곳에서 잘림, `# 제목` 머리말 조각 포함), CSV 붙음, `margin`이 같은 섹션의 두 조각을 1·2위로 세지 않고 top_k 밖 섹션까지 봄, 튜터 `evidence`로 recall@1·@3, block이 아닌 가드 후보 67행의 정답 섹션 1위 ≥64/67·3위 ≥66/67(R13 M5의 96%·99%, 질의 = 발화 + 답변. 이 정답 배정은 낙관적이다: 답변 4-gram 포함률로 골랐고 질의에도 그 답변이 들어 있다. 독립 기준인 튜터 `evidence` recall@1·@3을 함께 보고하고 67행 기준은 이식 회귀 확인에 쓴다), BM25 점수가 `common.py`의 `BM25.scores`와 같음(고정 질의 여러 개, 질의에 같은 2-gram이 반복되는 경우 포함, 허용 오차 1e-9), 검색 p95 ≤20ms, 색인 시간·메모리 출력. 67행의 정답 섹션은 `guardrail/scripts/r13/q5_ungrounded.py`의 배정 함수(`gold_sections_grounded`·`sections_with`)를 재사용하거나, 그것으로 만든 픽스처를 커밋해 쓴다.
- **수용 기준**: GR-03 수용 기준. 결과 수치를 PR에 적는다. **스킬**: `ecc:python-testing`, 에이전트 `ecc:rag-pipeline-reviewer`.

### S2.3 결정적 신호 (저신호·거절·숨긴 태그)

| GR / ADR | GR-06, GR-08 · [ADR-0014](../adr/0014-output-three-tier-and-safe-rule.md) | 우선·예상·모델 | P0 · 4h · Opus |
|---|---|---|---|
| 선행 | S2.1 | 브랜치 | `gr/s2.3-signals` |

- **만들 파일**: `src/guardrail/signals.py`(S1.2가 둔 `Signals` dataclass 옆에 함수), `tests/test_signals.py`.
- `is_low_signal(target_pre: str, has_history: bool, hidden: str) -> bool`: 자모·문장부호·이모티콘만(숫자·라틴·완성 음절 없음, 숨긴 텍스트 없음) **그리고** 앞에 대화가 있음. 1턴은 False(LLM으로). 입력 `target_pre`는 **자모 조합 전** 텍스트, 즉 `preprocess.strip_tags(target)`(S2.1, 태그 문자만 지운 것)다. 정규화본을 쓰면 "ㅋㅋㅠㅠ"가 음절로 조합돼 놓친다.
- `is_refusal(answer_raw: str) -> bool`: 거절·대안 제시 정규식(팀 작성. 시작점은 `guardrail/scripts/r13/extra_stats.py`의 `REFUSAL_RX`이고 **원문**에 적용, 붙인 뷰는 쓰지 않음). 적용 범위는 **후보 답변 원문에서 따옴표(“”, "", ‘’, '', 「」, 『』) 안 텍스트와 백틱·코드 블록 안 텍스트를 뺀 것**이다(ADR-0014. 이 뷰에서 R13-A 정규식은 거절 36/36, 순응 답변 오감지 0/19, [R13-A §6](../research/R13-public-set-analysis.md#6-규칙-②-오탐-재측정-santa-4라운드)). safe 규칙에만 쓰고 GR-07 ①에는 쓰지 않는다.
- `has_hidden_tags(answer_raw: str) -> bool`: 이모지 밖 U+E0000–E007F. 이모지 밖의 정의: 깃발 이모지 U+1F3F4(🏴) 바로 뒤에 이어져 U+E007F(종료 태그)로 끝나는 태그 문자열은 이모지이고, 그 밖의 태그 문자는 숨긴 것이다(종료 태그가 없으면 숨긴 것). [추론] SPEC §6의 튜터 답변 채점 지시 규정을 /guard safe에 적용한 것이다(PRD GR-06).
- 위기·PII·인젝션 어휘 목록은 만들지 않는다(GR-06).
- **테스트 먼저**: 400행 저신호 정밀도 100%(표본 안 값: 규칙이 이 400행의 관찰에서 나왔다. 비공개 일반화는 가정)·재현율 보고, "ㅋㅋㅠㅠ"(이력 있음)가 저신호, 따옴표·백틱 안 거절 문구는 안 걸림, 튜터 공개 세트 `refuse`·`redirect` 답변 36/36 검출, 순응 답변 오감지 수 보고(인용·코드 제외 뷰 0/19, 제외하지 않은 원문 1/19, R13-A §6.4), 태그 문자 예시("안녕" 뒤에 U+E0049 U+E0067 → True. 🏴 U+1F3F4 + U+E0067 U+E0062 U+E0065 U+E006E U+E0067 U+E007F(잉글랜드 깃발) → False. 🏴 뒤 태그에 종료 U+E007F가 없음 → True. 일반 글자 뒤 U+E007F 단독 → True), 거절 36/36은 표본 안(in-sample) 값임을 PR에 적음, LLM 없이 같은 값, KB md 48섹션(+붙인 CSV 23개, HTML 1개는 읽지 않음)의 코드 블록 안에서 신호 정규식(거절·숨긴 태그·저신호) 오매칭 0(GR-02 수용 기준, S2.1에서 옮김).
- 정규식은 팀이 쓴 `extra_stats.py`의 `REFUSAL_RX`를 시작점으로 다듬는다. 공개 세트 튜터 거절 답변 36건을 보고 쓴 것이라 36/36은 표본 안(in-sample) 값이다. 공개 세트 답변 문구를 통째로 따로 옮겨 적지 않는다(하드코딩 금지, GR-14). 출처는 provenance "팀 작성" 행.
- **수용 기준**: GR-06의 세 신호 정밀도·재현율 표와 GR-02 KB 코드 블록 오매칭 0을 PR에 적는다. **스킬**: `ecc:regex-vs-llm-structured-text`.

### S2.4 숫자·리터럴 규칙 ②

| GR / ADR | GR-06(추출), GR-07 ② · ADR-0014, ADR-0010 | 우선·예상·모델 | P0 · 5h · Opus |
|---|---|---|---|
| 선행 | S2.2 | 브랜치 | `gr/s2.4-literal-rule` |

- **만들 파일**: `src/guardrail/rules.py`, `tests/test_rules.py`.
- `extract_literals(text_raw) -> list[LiteralToken]`: `guardrail/scripts/r13/q5_ungrounded.py`의 `NUM_RX`를 옮기되 두 곳만 고친 기본 변형 **V2**다(`guardrail/scripts/r13/q6_rule_fp.py`의 `VARIANTS["V2"]`): 추출 전에 섹션 ID 인용 `\(?\b(?:PY|PR|FIN)-\d{2}-\d{2}\b\)?`을 지우고(`start`·`end`는 원문 위치로 되돌린다), ⓕ의 글자 클래스 `[한두세네다섯]`("다 번"에도 걸림)을 `(?:한|두|세|네|다섯)`으로 바꾼다. 토큰 종류: ⓐ 숫자(`.`·`,` 자릿수 허용) + 공백 0–1개 + 단위 `만 원·천 원·원·%·세·개월·점·번·개·씩·배·단계·권·명·회·줄` ⓑ `.`·`,`가 든 숫자(1.3, 1,000) ⓒ 두 자리 이상 수 ⓓ 숫자 리스트 리터럴(`[3, 7, 12]`) ⓔ 영문·코드 리터럴(`[A-Za-z_][A-Za-z_0-9.()]*`, 공백으로 이어진 영문 단어까지 한 토큰) ⓕ 한글 수사 `한·두·세·네·다섯` + 공백 0–1개 + `번·개·단계·가지·배`. `value`는 `scoring.normalize`(NFKC·소문자·공백·문장부호 제거) 값이고 **NFKC는 여기서 비교 양쪽에만** 건다. `rules.py`는 런타임에 `competition/grader`를 import하지 않고 `scoring.normalize`의 4줄 함수를 복사해 쓴다(provenance 기록). `start`·`end`는 원문 위치. 값 정규화(콤마·만·천·억), 단위 주·일·년·글자, 단위 없는 한글 수사, 백틱 안 영문만 보기는 기본이 아니고 ablation `rule_extra_tokens`다. 실측(V2): U 18/35, G 0/32, T 6/119([R13-A §6](../research/R13-public-set-analysis.md#6-규칙-②-오탐-재측정-santa-4라운드)). 원래 `NUM_RX`의 18/35·0/32는 가드만 본 기록이고 V0는 T에서 16/119가 오탐이다.
- `literal_rule(answer_raw, hits, margin, kb, cfg) -> tuple[str, ...] | None`: `margin`은 `guard.py`가 `kb.margin(query)`로 구해 넘긴다(`literal_rule`은 질의를 모른다). 답변 토큰의 `value`가 1위 조각의 **섹션 전체**(`kb.section_text(hits[0].section_id)`, 붙은 CSV 포함)를 `scoring.normalize`한 문자열에 부분 문자열로 없고, `margin ≥ rule_min_margin`(기본 3.5)이면 걸린다. `output.rule_emit_spans: false`면 걸렸을 때 빈 튜플을 돌려준다(`false/[]`, 결정은 S3.1의 규칙 정밀도 P). 구간 = 걸린 토큰을 `rule_span`(기본 `pm4` | `eojeol` 어절 경계, S3.1이 비교)까지 넓힌 **원문 부분 문자열**. 기본 `pm4`는 `q6_rule_fp.py`의 `merged_pm4_spans`와 같다: 걸린 토큰 중 서로 다른 문자열마다 답변에서 처음 나온 위치(`ans.find(t)`) 하나에 ±4자 창을 만들고(같은 토큰이 여러 번 나와도 첫 위치만 쓴다), 겹치는 창은 하나로 합치고, 같은 구간은 한 번만 낸다. 여기에 `scoring.normalize` 뒤 1글자 이상 남는 구간만 낸다는 필터를 더한다(추가 조건). 부분 문자열이 아니면 내지 않는다.
- `rule_require_same_kind`(ablation, 기본 꺼짐): 켜면 같은 종류의 다른 값이 그 섹션에 있을 때만 건다. 측정하지 않은 조건이라 기본 규칙에는 넣지 않는다. 종류 표: 단위 숫자 / 한글 수사 / 백틱 코드 / 함수 호출 / 리스트 리터럴.
- **테스트 먼저**: `NUM_RX` ⓐ–ⓕ 각 종류(스크립트와 같은 토큰), `rule_extra_tokens: [value_norm]`일 때만 1,000원 = 천 원 = 1000원, 겹치는 ±4 창 합치기·같은 구간 중복 제거·같은 토큰이 여러 번 나와도 첫 위치 창 하나·정규화 뒤 빈 구간 버리기, 복사한 `normalize`가 `scoring.normalize`와 같은 출력(테스트에서만 import해 임의 문자열·공개 세트 문자열로 대조), 정확한 이식의 목표값 = `q6_rule_fp.py`의 V2 값: `rule_min_margin` 0에서 U 18/35·G 0/32·T 6/119(T = 튜터 참조 답변 중 answer·scaffold·abstain이고 evidence 있는 119행), 3.5에서 16/35·0/32·3/119, 구간은 `merged_pm4_spans` 출력과 같은 스팬을 비교하고 구간 F1은 ±0.005 안(BM25 1위 0.651·18행 / 정답 섹션 0.638·16행). 18건 중 2행 pub-9b2c13b1 '5만 원'·pub-763823ae '3개월'은 BM25가 정답이 아닌 섹션을 골라 잡힌 것이다. 인용 ID 숫자("03")와 "다 번"이 토큰이 되지 않음, 모든 구간이 `answer` 부분 문자열. 합치지 않은 V0의 0.627은 기록이다.
- **수용 기준**: GR-07 ② 수용 기준. U·G·T 탐지·오탐을 따로 보고하고 위 V2 목표값과 같아야 한다(튜터 오탐 0은 요구하지 않음, 켜기·구간 결정은 S3.1의 정밀도 P), 부분 문자열 100%. **스킬**: `ecc:regex-vs-llm-structured-text`.

### S2.5 kNN·LR 보조 판정기

| GR / ADR | GR-05, GR-14 · [ADR-0013](../adr/0013-public-set-knn-and-leakage.md) | 우선·예상·모델 | P0 · 5h · Sonnet |
|---|---|---|---|
| 선행 | S1.4, S2.1 | 브랜치 | `gr/s2.5-knn-lr` |

- **만들 파일**: `src/guardrail/knn.py`, `src/guardrail/classifier.py`, `tests/test_knn.py`, `tests/test_classifier.py`.
- `KnnIndex(rows: Sequence[LabeledRow], cfg)`: `TfidfVectorizer(analyzer="char_wb", ngram_range=(1,3))` 코사인. `neighbors(text, k)`, `majority(text) -> Category`(이웃 `input.knn_vote_k`개, 기본 7), `fewshot(text) -> list[LabeledRow]`.
- few-shot 규칙(GR-05): 코사인 상위에서 4–8개. 우선순위: ① block/allow 반대쪽 최근접 1개(kNN 1위 이웃의 block/allow 판정과 판정이 다른 예시 중 가장 가까운 것)는 반드시 넣는다 ② 범주당 최대 2개 ③ 전체 최대 8개 ④ 짝(같은 묶음) 문항의 짝은 ②·③을 지킬 때만 더한다. 가장 비슷한 예시를 마지막에. 예시는 발화 + 범주만. 짝째로 넣으려면 기동 때 묶음 정보가 필요하다. `KnnIndex`가 기동 때 `src/guardrail/clusters.py`의 공용 함수(S1.4, `eval.cv`도 import)로 묶음을 직접 계산한다. 서버는 eval 패키지에 의존하지 않는다.
- `LrClassifier(rows, cfg)`: 같은 특징의 `LogisticRegression`, 기동 때 학습. `predict(text) -> Category`. pickle·파일 저장 없음. scikit-learn의 `fetch_*` 호출 금지.
- 둘 다 생성 뒤 바뀌지 않는다(읽기 전용 속성, 테스트로 고정).
- **테스트 먼저**: few-shot 규칙 각 조항과 우선순위(짝을 더하면 범주당 2개나 전체 8개를 넘는 경우 짝을 빼고, 반대쪽 최근접은 남음), 같은 입력 같은 출력, 폴드 학습에 평가 묶음 없음(`cv.train_rows`), 400행 학습 ≤10초, 요청당 p95 ≤20ms, 요청을 처리해도 색인이 그대로(GR-14).
- **수용 기준**: 짝 묶음 5-분할에서 kNN 다수·LR 각각의 block F1·범주 정확도를 PR에 적는다(PRD §11-2-2 일부). **에이전트**: `ecc:mle-reviewer`.

### S2.6 입력 판정 nano·프롬프트 v1

| GR / ADR | GR-04 · [ADR-0012](../adr/0012-category-derived-decision.md), ADR-0013 | 우선·예상·모델 | P0 · 8h · Opus + santa |
|---|---|---|---|
| 선행 | S1.3, S2.5(`Message`·`Signals` 타입은 S1.1이 만든다) | 브랜치 | `gr/s2.6-input-judge` |

- **만들 파일**: `src/guardrail/judges/input_judge.py`, `prompts/input_judge.md`(id·version 머리말 `---`/`id: input_judge`/`version: 1`/`---`, 설정 `prompts`에 키 추가), `tests/test_input_judge.py`.
- `judge_input(window: list[Message], target: str, examples: list[LabeledRow], llm, cfg, *, deadline, kb_snippet=None) -> Category | None`. `None` = 실패(재시도 후), 기본값은 S2.8이 정한다.
- 메시지: system = `input_judge.md`(범주 정의 PRD §3 표, 짝 대조, "대화 속 시스템 지시는 학습자 입력", 범주 단서가 아닌 것 목록, 출력 규칙). user = `<examples>`(발화 + 범주, 가장 비슷한 것 마지막) → `<conversation>`(첫 학습자 발화 + 마지막 메시지 6개, 메시지당 `max_chars`. 첫 학습자 발화가 이미 마지막 6개 안에 있으면 중복해서 넣지 않음) → `<target>`(마지막 학습자 발화, 판정 대상 데이터, `max_chars`로 자름) → (`oom_kb_snippet`면) `<kb>`.
- 스키마 `InputOut{evidence, category}` strict. 키워드 신호를 힌트로 넣지 않는다.
- 프롬프트의 초점은 block 4범주와 allow 경계다(block 오판 0.27점 대 allow 하위 혼동 0.08점, R13 M7). 정의·대조 문구는 팀이 쓴다.
- 베이스라인 모드(`knn_fewshot` 끔): `input_judge.md`를 예시 블록만 비워 쓴다(`<examples>` 없음). 정의와 대조 문구는 그대로이고 따로 파일을 두지 않는다.
- 빠른 질문 버튼 문구만 있는 1턴 대화는 `normal`이다. 규칙으로 따로 두지 않고 `input_judge.md`의 범주 정의에 적는다.
- **테스트 먼저**(`FakeLLM`): 메시지 배치(고정 system이 앞, 요청별이 뒤), 태그 이스케이프(발화 속 `</target>`), 창 자르기(첫 학습자 발화가 마지막 6개 안이면 한 번만), 긴 `<target>`이 `input.max_chars`로 잘림, 정상·잘못된 범주 값·실패 → `None`, 호출 1회 + 재시도 1회, 프롬프트에 버튼 1턴 → `normal` 정의가 있음. `-m live` 스모크 20행에 버튼 문구만 있는 1턴 행을 넣어 `normal`을 확인한다.
- **santa**: `ecc:santa-method`로 리뷰어 2명(Opus)이 프롬프트를 PRD §3·SPEC §3과 대조. 둘 다 통과해야 머지.
- **수용 기준**: 위 테스트, 파싱 성공률 100%(기본값 포함), `-m live`로 20행 스모크 시 호출 p95 ≤5s. 정확도 수치는 S2.9가 낸다.

### S2.7 출력 근거 판정 nano·프롬프트 v1

| GR / ADR | GR-07 ③ · ADR-0014 | 우선·예상·모델 | P0 · 5h · Opus + santa |
|---|---|---|---|
| 선행 | S1.3, S2.2 | 브랜치 | `gr/s2.7-output-judge` |

- **만들 파일**: `src/guardrail/judges/output_judge.py`, `prompts/output_judge.md`, `prompts/output_judge_baseline.md`, `tests/test_output_judge.py`.
- `ask_grounded(target: str, answer_revealed: str, hits: list[Hit], llm, cfg, *, deadline, baseline=False) -> OutputOut | None`. `baseline`은 `output.mode: baseline`일 때 참이다.
- 메시지: system = `output_judge.md`("근거는 KB만, 자료와 모순되지 않는 일반 설명은 근거 있음, 과목 고유 사실을 지어냈거나 KB와 모순되면 근거 없음", SPEC §5·§6. [추론] "일반 설명은 근거 있음"은 SPEC §6의 튜터 채점 기준을 가드 grounded에 옮긴 것, 가상 설정은 KB 기준). user = `<question>` → `<answer>`(숨긴 문자를 드러낸 원문, `answer_max_chars`) → `<kb id="섹션 ID">` × 3.
- 스키마 `OutputOut{reason, grounded}`. 베이스라인 변형은 `OutputBaselineOut{reason, grounded, safe}`(GR-11 "safe는 nano").
- 구간은 내지 않는다(nano 구간은 S3.5 ablation).
- **테스트 먼저**: 메시지 배치, 답변 속 숨긴 지시가 드러난 채 들어감, 태그 이스케이프(후보 답변·KB 텍스트 속 `</answer>`·`</kb>`가 닫는 태그로 작동하지 않음. S2.6의 `</target>` 테스트와 같은 방식), 실패 → `None`.
- **santa**: S2.6과 같은 규칙.
- **수용 기준**: 위 테스트. grounded 정확도·"근거 없음" 정밀도는 S2.9·S3.1이 낸다.

### S2.8 판정 조립: decision·3단·safe·기본값·예산

| GR / ADR | GR-07, GR-08, GR-09, GR-01 · ADR-0012, ADR-0014, ADR-0009 | 우선·예상·모델 | P0 · 5h · Opus |
|---|---|---|---|
| 선행 | S1.2, S2.3, S2.4, S2.6, S2.7 | 브랜치 | `gr/s2.8-assemble` |

- **만들 파일**: `src/guardrail/decide.py`, `src/guardrail/guard.py`, `tests/test_decide.py`, `tests/test_guard.py`. `guard.py`에는 S1.2가 둔 `Judgement`·`ConstantGuard` 옆에 `Guard`를 더하고, `server.py`의 `ConstantGuard`를 `Guard`로 바꾼다. 계약은 그대로 `Guard.judge(req) -> Judgement`이고 서버는 `judgement.response`만 직렬화한다.
- `decide.py`(순수 함수):
  - `decision_of(category, cfg.block_categories)`(schema.py), `fallback_category(knn_major, lr_pred)`: 같으면 그 범주, 다르면 LR. LR이나 kNN(`knn_vote`)이 꺼졌으면(`None`) 있는 쪽, 둘 다 없으면 `normal`(GR-09).
  - `output_verdict(category, sig, rule_spans, nano, toggles, failure_default) -> (grounded, unsupported, tier)`: ① block 범주 → `true/[]`(거절 정규식은 여기 쓰지 않는다. `output.mode: v6`에서 토글 없이 늘 적용) ② 규칙 걸림 → `false/구간`(`rule_emit_spans: false`면 `false/[]`) ③ `nano_tier3`가 켜졌고 nano가 근거 없음 → `false/[]` ④ 그 밖. 꺼진 토글의 단(②·③)은 건너뛴다. ①은 토글로 끄지 않는다. 출력 nano 실패(`None`)면 ③을 건너뛴다. ④의 값: `output.failure_default`(`grounded` = `true/[]` | `ungrounded` = `false/[]`, 기본 `grounded`)는 출력 nano가 실패했을 때와 `nano_tier3`가 꺼져 nano 판정을 쓰지 않을 때의 ④에만 적용한다. `nano_tier3`가 켜져 있고 nano가 근거 있음이라고 한 ④는 늘 `true/[]`다. 값은 S3.1이 최종 설정에서 정한다.
  - 빈 후보 답변(`""`)은 `true/[]`, `safe=true`이고 출력 nano를 부르지 않는다.
  - `output.mode: baseline`이면 위 대신 nano 베이스라인 값을 grounded·safe에 바로 쓰고 `unsupported=[]`(실패면 `true/[]`·`safe=true`).
  - `safe_of(category, sig, toggles)`: `safe_rule`이면 `not(block) or refusal`, 숨긴 태그면 False. 끄면 True(베이스라인 모드의 safe는 위에서 nano 값).
- `guard.py`: [master-plan §4.2](../master-plan.md#42-판정-흐름-guardpy) 순서. 전처리 전에 메시지 하나와 후보 답변을 각각 `input.max_chars`로 자르고(원문 길이 상한), BM25 질의는 학습자 발화 `retrieval.max_query_chars // 2` + 후보 답변 `retrieval.max_query_chars // 2`, kNN·LR 입력은 대상 발화 `retrieval.max_query_chars`(기본 4000)로 자른다. CPU가 큰 단계는 스레드 풀에서 돌린다. 출력 nano는 후보 답변이 비어 있지 않으면 부른다(v6 모드에서 `nano_tier3`와 무관, `Judgement.nano_grounded`에 원값 기록. 단 `nano_tier3: false`이고 `output.call_nano_when_unused: false`면 건너뜀, 기본 true). `asyncio.wait`로 ⑤·⑥을 마감까지, 넘으면 취소 후 기본값. `Judgement`를 채운다. `Guard.build`는 기동 때 `llm.probe()`를 부른다.
- **테스트 먼저**: GR-07 표 ①–④ 각 행(block이 아닌 행의 거절 답변은 ①이 아님), `safe_rule: false`여도 ①은 적용, `failure_default` 두 값 각각에서 출력 nano 실패 → ④가 그 값, `nano_tier3: false` → ④가 그 값, `nano_tier3: true`이고 nano가 근거 있음 → `failure_default`와 무관하게 `true/[]`, `nano_tier3: false`여도 `call_nano_when_unused: true`(기본)면 출력 nano 호출 1회와 원값 기록, `call_nano_when_unused: false`면 호출 0, 베이스라인 모드 매핑, GR-08 safe(태그 포함), GR-09 표 4행(입력 실패·출력 실패·예산 초과·파싱 실패. `candidate_answer`가 문자열이 아닌 경우 포함: `failure_default`·safe는 GR-08)과 LR·kNN 중 하나가 꺼진 입력 실패, 빈 `candidate_answer` → `true/[]`·`safe=true`, 저신호면 입력 nano 호출 0, 후보 답변 없으면 출력 nano 호출 0, `FakeLLM` 장애 주입(타임아웃·5xx·429·잘못된 JSON)으로 공개 세트 400행 → 형식 오류 0·모든 응답이 `budget_s` + 1초 안, 1MB 이력·1MB 마지막 발화·4000자 넘는 후보 답변 요청 → 1MB 요청이 `budget_s` 안에 끝나고, BM25가 받은 질의의 학습자 부분과 답변 부분이 각각 `retrieval.max_query_chars // 2` 이하이며 kNN·LR이 받은 텍스트가 `retrieval.max_query_chars` 이하, 입력 nano의 `<target>`이 `input.max_chars` 이하(CPU 작업이 예산을 밀지 않음).
- **수용 기준**: GR-09 수용 기준. 리뷰 `ecc:silent-failure-hunter`, `ecc:python-reviewer`.

### S2.9 평가 하네스 v1·베이스라인

| GR / ADR | GR-13, GR-11 · ADR-0006, ADR-0013 | 우선·예상·모델 | P0 · 6h · Sonnet |
|---|---|---|---|
| 선행 | S1.4, S2.8(S2.8 전 골격은 S1.4 뒤 `--fake`로 S1.2의 `ConstantGuard`(`Judgement` 반환)를 대상으로 돈다) | 브랜치 | `gr/s2.9-harness` |

- **만들 파일**: `src/guardrail/eval/run.py`, `eval/stats.py`(묶음 부트스트랩 추가), `tests/test_run.py`, `configs/ablation/*.yaml`(토글마다 하나).
- CLI는 [master-plan §4.6](../master-plan.md#46-평가-cli-eval). 폴드마다 `Guard.build(rows=cv.train_rows(fold=k))`, 그 폴드 행의 `judge()`를 동시 16으로 부른다. `--repeat 3`, `--estimate`, `--max-calls`, `--fake`, `--set`, `--ablation`, `--holdout`(열람 기록·4회째 거부).
- `report.md`: master-plan §4.6 목록 전부 + 튜터 200행 범주 정확도 따로.
- **첫 실측**(API): `default.yaml` × 3회, `baseline.yaml`(토글 모두 끔 + `output.mode: baseline`) × 3회, kNN 다수·LR 단독(호출 0). 표를 `results/2026-11-15-v1.md`로 커밋.
- **테스트 먼저**: 골격 단계는 `ConstantGuard`로 400행(판정기 생성 함수를 주입), S2.8 머지 뒤 `--fake`로 `Guard` 400행 끝까지(호출 0) 테스트를 더한다, 예상 호출 수 = 실제 호출 수, 상한 초과 시 시작 전 멈춤, 홀드아웃 4회째 거부, 부트스트랩이 묶음 단위로 뽑음.
- **수용 기준**: GR-13 수용 기준(표 5종, 3회 분산), GR-11(설정만으로 ablation 실행). **스킬**: `ecc:eval-harness`.

### S2.10 부하 시험·v1 제출

| GR / ADR | GR-10, GR-09 · ADR-0011 | 우선·예상·모델 | P0 · 3h · 본인(코드 초안은 Sonnet) |
|---|---|---|---|
| 선행 | S1.6, S2.8 | 라벨 | + `orchestrator` |

- **만들 파일**: `src/guardrail/eval/load.py`(stdlib `urllib` + 스레드 풀 또는 `asyncio`, 공개 세트 요청을 다시 보냄), `tests/test_load.py`(로컬 `create_app` 대상).
- 런박스에서 동시 64·약 600요청(가드 300 + 튜터 300, 튜터가 없으면 가드 600). 시간 초과·형식 오류·3xx·p50·p95·전체 시간. 채점 수준을 넘는 시험은 하지 않는다.
- 확인: BM25·TF-IDF·LR 같은 CPU 작업이 이벤트 루프를 막지 않는다. 스레드 풀(`asyncio.to_thread` 등)로 돌리거나 요청당 1ms 미만임을 동시 64에서 잰다. 1MB 이력 요청 몇 개를 섞어 보내도 다른 요청의 시간 초과가 0이고 p95가 늘지 않는지 함께 본다(상한 `retrieval.max_query_chars`·`input.max_chars`).
- 통과하면 v1 제출(커밋 해시, 같은 런박스의 `/tutor` 포함, master-plan §3). 결과를 `results/2026-11-15-load.md`. 11/15 밤에 못 끝나면 v1 제출은 11/16 오전으로 밀 수 있다(완충).
- **수용 기준**: GR-10 수용 기준(시간 초과 0, 가드 p95 ≤8s 목표, 전체 약 2분 수준). 넘으면 게이트·`budget_s`·`read` 한도를 조정하는 PR. **스킬**: `ecc:benchmark`.

### S2.11 튜터 공유 API (제안 T1·T2·T4)

| GR / ADR | GR-10, PRD §9 | 우선·예상·모델 | P1 · 2h · Sonnet |
|---|---|---|---|
| 선행 | S2.8 | 라벨 | + `orchestrator` |

- `Guard.classify(messages) -> Category`, `async Guard.judge_output(messages, candidate_answer, category: Category | None = None) -> OutputVerdict`(PRD §9 T1-2. `category`가 None이면 먼저 `classify(messages)`를 부른다(nano 호출 +1). 3단 ①과 safe는 그 범주를 쓴다), `KbIndex` 재사용, `LLM.slot()`을 팀 서버에서 import하는 예제(`tests/test_tutor_api.py`). 범주 → action 대응(PRD §9 T3)을 docstring에 둔다. 리더에게 PR 링크로 전달한다. `/tutor` 코드는 고치지 않는다.

## 4. Phase 끝 점검

- [ ] 관문 ①–④ 판정, 결과 표 커밋(`results/2026-11-15-v1.md`)
- [ ] master-plan §4와 코드 15분 대조(서명·설정 키·CLI), 다르면 master-plan 먼저 고침
- [ ] provenance에 신호·정규식·프롬프트·kNN·LR 행이 있는지 확인(GR-14)
- [ ] C3 이슈 생성, C2 에픽 체크리스트 갱신
