# 서버 자산 출처 (provenance)

작성 2026-11-09 · v6 갱신 2026-11-10 · 담당 danielcho02 · 근거: `competition/SPEC.md` §7.1·§8 · [PRD v6 GR-14](../PRD.md#gr-14-규칙-준수-p0) · [R12 §8](../research/R12-competition-baseline.md#8-v4-자산-출처-점검-gr-13)

서버(채점받는 `/guard`)에 들어가는 데이터·사전·예시·프롬프트의 출처를 적는다. 시연에서 코드와 함께 보여 준다. **새 자산을 서버에 넣는 PR은 이 표에 행을 더해야 한다.**

허용: 배포 자료와 그것을 가공한 것 · 팀이 직접 쓴 것(프롬프트·규칙 안의 예시 포함) · 외부 호출 없는 오픈소스 라이브러리.
불가: 미리 학습된 모델 파일 · 인터넷에서 받은 사전·목록·데이터셋 · 다른 LLM으로 만든 별도 예시·데이터 묶음 · 다른 팀 코드·프롬프트.

## 1. 서버에 들어가는 것

| 자산 | 출처 | 작성 | 상태 |
|---|---|---|---|
| `src/guardrail/preprocess.py` 자모 변환표·정규화 규칙 | 유니코드 한글 조합 규칙(공개 사양) | 팀(R5 조사 단계 참고 코드를 옮겨 고침, ADR-0001) | 있음 |
| `kb/<과목>/` 색인(문자 2-gram BM25) | 대회 배포 자료 | 주최측 | 기동 때 색인(PRD v6 GR-03) |
| kNN 색인·few-shot 예시 | `public/guard.jsonl`·`public/tutor.jsonl` 400행(배포 자료). SPEC §7.1이 공개 세트 few-shot·유사도 검색을 허용 | 주최측 | 기동 때 색인(GR-05, ADR-0013). 요청 처리 중 바뀌지 않음 |
| `src/guardrail/clusters.py`의 빠른 질문 버튼 문구 목록(8종, 짝 묶음에서 제외) | 공개 세트 튜터 첫 발화(배포 자료)에서 뽑음([R13-A §1.4](../research/R13-public-set-analysis.md#14-빠른-질문-버튼-첫-발화-spec-2)) | 주최측 문구, 팀이 목록화 | S1.4. `KnnIndex`가 기동 때 묶음 계산에 씀 |
| 설정 `block_categories` 표(범주 → decision) | 공개 세트 라벨(배포 자료, 200/200에서 범주의 함수, R13 M1) | 팀(라벨에서 도출) | ADR-0012. `configs/default.yaml` |
| 문자 n-gram LR | 위 400행으로 기동 때 처음부터 학습(SPEC §7.1 허용). 학습된 모델 파일은 저장소에 두지 않음 | 팀(코드) | GR-05 |
| 결정적 신호: 거절 정규식, 숫자·단위·한글 수사 표, 저신호 판정, 숨긴 태그 문자 | — | 팀이 직접 씀 | GR-06. 다른 LLM이 만든 목록을 붙여 넣지 않는다 |
| 거절 정규식 시작점: `guardrail/scripts/r13/extra_stats.py`의 `REFUSAL_RX` | 공개 세트 튜터 거절 답변 36건을 보고 팀이 쓴 분석 스크립트의 정규식. 36/36은 표본 안(in-sample) 값 | 팀 | GR-06. 서버 모듈로 옮겨 다듬음(S2.3, council D14: `할 수 없`·`안 돼요`·`하지 않아요`·`하지 말` 삭제, 토큰 사이 공백 `\s*`, `못\s+해요`). 인용·코드를 뺀 뷰(`q6_rule_fp.py`의 `QUOTE_CODE_RX` 그대로)에 적용 |
| `rules.py`의 `normalize` 4줄 | `competition/grader/scoring.py`의 `normalize`를 복사(배포 자료의 코드) | 주최측 | GR-06. 런타임에 `competition/grader`를 import하지 않음. 단위 테스트가 `scoring.normalize`와 같은 출력인지 대조(S2.4) |
| 판정 프롬프트 `prompts/input_judge.md`·`prompts/output_judge.md`(범주 정의·짝 대조 문구) | 공개 세트를 보고 팀이 씀 | 팀 | GR-04·GR-07 |
| 입력 판정 영어 프롬프트 `prompts/input_judge_en.md` 2.0(경계 6블록, 지어낸 한국어 최소쌍 예문) | 교차 검증 폴드 0–2의 오판 28행과 웹 조사(R15)를 보고 팀이 씀 | 팀(AI 코딩 도구 보조). Opus 적대 검토 1회 반영 뒤 문구 고정 | S3.2. 공개 발화와 8어절 겹침 0(`test_prompts_en.py`) |
| 탈옥 2차 판정 프롬프트 `prompts/jb_check_en.md` 1.1(순서형 점검, 한국어 어미 단서) | 공개 세트에서 오탐한 normal 11행과 정답 탈옥 14행을 보고 팀이 씀. 한국어 단서(2–4어절 어미 조각)는 이 행들의 문구에서 뽑음 | 팀(AI 코딩 도구 보조). 25행 선별 1회(1.0) 뒤 한 번 고침, 적대 검토 없음 | S3.3, `toggles.jb_check`. 8어절 겹침 0(테스트), 짧은 조각은 그 테스트로 못 잡음 |
| 베이스라인 프롬프트 `prompts/output_judge_baseline.md`(reason → grounded → safe, `output.mode: baseline`) | 팀이 씀 | 팀 | GR-11 베이스라인. 입력 베이스라인은 `input_judge.md`에서 예시 블록만 비운 것이라 따로 파일이 없다 |
| (쓰면) nano 합성 예시 | 운영진 제공 `gpt-5-nano`(SPEC §7.1 허용). 생성 스크립트와 함께 커밋 | 팀 + nano | 기본 안 씀(ADR-0013) |
| 라이브러리 | PyPI: `openai`, `pydantic`, `pyyaml`, `fastapi`, `uvicorn`, `scikit-learn`(+`scipy`), `rapidfuzz` | 오픈소스, 모델·사전 파일 없음, 외부 호출은 운영진 엔드포인트만. `tiktoken`·Kiwi는 쓰지 않음 | [R13-B2](../research/R13-B2-libraries.md) |

## 2. 서버에 넣지 않는 것 (개발·평가 전용)

| 자산 | 출처 | 이유 |
|---|---|---|
| `data/seed/smoke_benign.jsonl`(50), 로컬 `smoke_attack.jsonl`(50)·`smoke_crisis.jsonl`(30) | Claude Code 서브에이전트(Sonnet) 초안 + 본인 검토. `data/README.md`에 명시, 레코드 값은 `source: "self"` | 다른 LLM이 만든 데이터 묶음 |
| `data/seed/textbook_placeholder.jsonl`(38절) | 같음 | 같음. v5에서는 `kb/`로 대체 |
| `data/seed/grounding/inject_prompt.md` | 팀 작성 프롬프트(산출물은 LLM 생성) | 산출물이 LLM 생성 데이터 |
| `prompts/attack_judge.md` | 팀 작성(AI 코딩 도구 보조) | v4 평가 전용, S0–S14 기준 |
| `resources/crisis_resources.yaml` | 공공 상담 번호 + 팀 문구, 기사 인용 | 가드 응답에 문구가 없어 불필요 |
| 외부 공개 데이터셋(wildguardmix, MultiJail 등) | 인터넷 | 저장소에 없음. 서버 밖 평가 사용도 운영진 확인 전에는 하지 않음 |
| `configs/*.yaml`의 `typesafe-ai/jev-1.14.0` | 외부 모델 | 코드 재계획에서 삭제 |
| `guardrail/scripts/r13/` 분석 스크립트 | 공개 세트 로컬 분석(R13-A) | 개발 전용 |
| `src/guardrail/eval/splits/v1.json`(짝 묶음·폴드·홀드아웃 분할) | 공개 세트에서 만든 분할 파일(S1.4) | 개발 전용. 서버는 읽지 않는다(하네스만 씀) |

## 3. 시연 전 확인

- [ ] 위 1절에 없는 데이터·목록 파일이 서버 경로에 없다
- [ ] 서버 로그에 요청·답변 본문이 없다(PRD GR-12)
- [ ] `.env`·키가 git에 없다
- [ ] 제출한 커밋 해시가 `submission.json`의 `commit`과 같다
