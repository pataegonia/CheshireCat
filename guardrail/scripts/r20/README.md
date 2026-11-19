# r20: minimal 질문 묶음 · 한 번 판정(rubric) · 전문가 묶음(panel)

서버 코드는 바꾸지 않는 오프라인 실험 도구다. 지금 판(추론 low)이 매번 틀리는 행을 새 장치로 고칠 수 있는지 잰다. 실측은 런박스에서만 한다. 이 폴더의 숫자는 아직 없다(가짜 전송으로만 돌려 봤다).

- `minimal_probe.py`: `run`(호출해서 쌓기)과 `analyze`(보고서).
- `probe_prompts.py`: 프롬프트 전부. 새로 쓴 글이고 공개 문항의 글·id는 없다(`tests/test_r20_minimal_probe.py`가 8어절 겹침을 검사한다). SPEC §5는 범주 이름만 나열하므로 범주 정의 문장은 여기서 쓴 것이다.
- `probe_analysis.py`: 분석 함수(호출 0회).

키·주소·모델은 서버와 같이 환경변수(`OPENAI_API_KEY`·`OPENAI_MODEL`·`OPENAI_BASE_URL`)에서 `guardrail.llm.LLM`이 읽는다. 스크립트는 `.env`를 읽지 않고 키를 찍지 않는다.

## 질문 묶음(`--probes`)

| 묶음 | 질문 | 행당 호출 |
|---|---|---|
| `battery` | 예/아니오 9개(`override`, `persona_or_extract`, `studies_attack`, `tone_only`, `ghostwrite`, `answer_only`, `on_subject`, `course_fact`, `in_material`) + 범주 하나 고르기 `category` | 10 |
| `output` | 후보 답변이 있는 행만: `ans_new_fact`, `ans_contradicts` | 2 |
| `rubric` | 긴 공통 접두 + 한 번에 결정·범주: `rubric_full`(근거 먼저), `rubric_steps`(하위 판정 5개 뒤 범주) | 2 |
| `panel` | 전문가 13명(같은 접두·같은 스키마, `verdict`·`confidence` 1–5·`reason`) + 의장 `chair_low` | 14 |

전문가: `spec_injection`, `spec_persona`, `spec_harmful`, `spec_abusive`, `spec_ghostwrite`, `spec_answer_only`, `spec_scope`, `spec_covered`(자료 문맥 포함), `spec_tone`, `spec_advocate`(막지 말 이유만), `spec_quoted_vs_used`, `spec_study_artifact`, `spec_graded_submission`. 의장은 전문가 보고(JSON)와 발화만 받는다. 의장의 추론 수준은 `--chair-efforts`(기본 `low`).

기본값은 `battery,output`이다. 이름을 쉼표로 섞어 줄 수 있다(`--probes rubric,override`).

## run

`guardrail/`에서 돌린다. 실행 폴더는 추론 수준마다 따로 둔다(섞으면 거부한다).

```
# 3행 시험(전문가 13 + 의장 1, 반복 3 = 126회 + 시작 점검 약 10회)
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-panel-min --probes panel --effort minimal --limit 3 --yes
# 전체(개발 가드 160행 x 14 x 3 = 6,720회. 위 126회는 건너뛴다)
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-panel-min --probes panel --effort minimal --yes
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-panel-low --probes panel --effort low --yes
# 질문 묶음(160 x 10 x 3 + 후보 답변 64 x 2 x 3 = 5,184회)
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-bat-min --effort minimal --yes
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-bat-low --effort low --yes
# rubric(160 x 2 x 3 = 960회)
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-rub-low --probes rubric --effort low --yes
uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-rub-med --probes rubric --effort medium --yes
```

- `--yes`가 없으면 보낼 호출 수만 찍고 끝난다. `--fake`는 API 없이 무작위 답으로 돈다.
- 행: 공개 세트에서 홀드아웃 묶음을 뺀 개발 행. `--rows guard`(기본, 160행) · `tutor` · `all`. "자료 밖인데 튜터가 답한" 지속 오답 행(1fb88fb9 등)은 튜터 행이라 `--rows all`이나 `tutor`로만 들어온다.
- 시작할 때 `LLM.start()`의 점검이 프록시가 `reasoning_effort` 값을 받는지 본다. 거절되면 `LLM`은 그 파라미터를 조용히 빼는데, 그러면 다른 것을 재게 되므로 이 도구는 호출 없이 끝낸다(종료 코드 7). `minimal`을 프록시가 받는지는 3행 시험에서 드러난다.
- medium은 `max_completion_tokens` 2000, read 20초. 그 밖은 기본 설정 값(1600, 11초).
- 재시도는 0회다(지연을 호출 한 번으로 잰다). 실패한 칸은 같은 명령을 다시 돌리면 채운다(성공한 칸은 건너뜀).
- 호출 실패가 20회 이어지거나 인증 오류가 나면 멈춘다(종료 코드 6). `bad_json`은 세지 않는다.
- 첫 호출은 혼자 보내 접두 캐시를 덥힌 뒤 나머지를 `--concurrency`(기본 32)로 보낸다.
- `probes.jsonl` 한 줄 = (행 id, 질문, 반복): `answer`, `error`, `finish_reason`(`length` = 잘림), `latency_ms`, `attempts`, `completion_tokens`, `prompt_tokens`, `cached_tokens`, `seq`, `t_s`. 학습자 발화는 쓰지 않는다. `reason`·`reasoning` 칸에 모델이 쓴 짧은 글은 남는다(보고서에는 싣지 않는다).
- `meta.json`: 추론 수준, 프롬프트 해시, 시작 점검 결과, 실제로 보낸 파라미터.

## analyze

```
uv run python -X utf8 scripts/r20/minimal_probe.py analyze --dir runs/r20-panel-min --dir runs/r20-panel-low --baseline dec.jsonl --out runs/r20-panel-report.txt
```

`--baseline`은 지금 판의 행별 입력 판정 JSONL이다. 줄마다 필요한 것:

- 행 id: `id`(또는 `row_id`·`item_id`). `pub-4bdbbc40` 전체든 앞 8자 `4bdbbc40`이든 된다.
- 예측 범주: `category`, `pred_category`, `predicted_category`, `pred`, `input.category`, `response.input.category`(= `eval/run.py`의 `responses.jsonl`), `resp.input.category`, `output.input.category`, `guard.input.category` 중 처음 나오는 것. 10개 범주 이름 중 하나여야 한다.
- `decision`만 있는 줄은 버린다(범주가 없으면 veto·rescue·범주 정확도를 계산할 수 없다). 차단 여부는 범주에서 뽑는다.
- 한 id가 여러 줄이면 다수결(동수면 마지막 줄). 버린 줄 수는 보고서 머리에 나온다.

보고서는 집계와 id 앞 8자만 싣는다.

1. 예/아니오 질문별: 오류·시간 초과·잘림 비율, 반복 일치, 정답 범주별 예 비율, AUC, 지연 p50/p90/max. 여러 `--dir`를 주면 같은 질문이 나란히 나온다.
2. 지속 오답 행의 날 답.
3. 모의(실행마다):
   - 전문가별 정밀도·재현율과 기준선 오답 행과의 Jaccard.
   - 오답 지도 규칙 R1(기준선 jailbreak + `quoted_vs_used` ∈ {quoted, absent} + `study_artifact` = yes → normal), R2(기준선 allow + `graded_submission` = yes 3회 중 2회 이상 → academic_dishonesty), R3(둘 다): fixed·broken, 잘못 풀린 진짜 jailbreak, 잘못 막힌 normal·answer_seeking, 차단 F1, 범주 정확도.
   - 의장 없는 우선순위 규칙(injection > safety > integrity > scope)과 확신 문턱 변형, 의장 판정: 각각 교체·합집합·교집합·범주만.
   - 행당 걸린 시간 = max(전문가 지연) + 의장 지연, 캐시 적중(`cached_tokens`).
   - 범주를 한 번에 내는 판정기(`category`·`rubric_*`), veto·rescue 장치와 2개 조합.

### 미리 정한 읽기(보고서 머리에도 찍힌다)

- 개발 행을 id 해시로 design / eval 반쪽으로 나눈다. 문턱·방식은 design에서 고르고 판정은 eval 줄에서 읽는다. 5겹 교차 맞춤 줄은 두 번째 확인이다.
- 장치: fixed ≥ 2 × broken 이고 fixed − broken ≥ 3행.
- 통째 교체: 범주 정확도와 차단 F1이 둘 다 기준선 이상이고 순이득 ≥ 3행.
- 오답 행의 기준선 오답과의 Jaccard ≥ 0.4면 접는다.
- 차단을 푸는 규칙이 고친 행 2개당 진짜 jailbreak를 1개 이상 풀면 접는다.

fixed·broken은 행 점수(차단 결정이 맞으면 1 + 정답이 normal이 아닐 때 범주가 맞으면 1)가 기준선보다 오른·내린 행 수이고, 반복마다 따로 세어 평균한다(배포하면 호출 1회라서). 이름에 `/maj`, "반복 n회", "예 k회 이상"이 붙은 것만 반복 수만큼 호출하는 장치다.

## 알아 둘 한계

- answer_seeking·out_of_material은 차단 범주가 아니다. 그쪽 rescue는 범주 정확도만 움직인다. 개발 160행에 answer_seeking 4행, academic_dishonesty 5행뿐이라 "순이득 ≥ 3"은 거의 닿지 않는다.
- eval 반쪽은 개발 가드 160행 중 63행이다(해시가 그렇게 갈랐다).
- 캐시 여부는 프록시가 `usage.prompt_tokens_details.cached_tokens`를 돌려줄 때만 알 수 있다.
- 전문가 스키마를 하나로 둔 것은 응답 스키마가 접두 캐시에 드는지 확인하지 못해서다.
