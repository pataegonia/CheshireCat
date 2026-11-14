# 입력 프롬프트 v1 단독 실측 3회 (R14-D1)

작성 2026-11-13 · 담당 danielcho02 · 근거 계획: [R14 §6](../docs/research/R14-prompt-uplift.md#6-council-2026-11-13-후보-선별) R14-D1과 "첫 실측 뒤 조정"

입력 프롬프트 v1(`input_judge.md` version 1)의 기준선이다. **v2는 이 3회 범위와 비교한다.**

## 실행

| 항목 | 값 |
|---|---|
| 어디서 | 런박스(`openai/gpt-5-nano`, `reasoning_effort: low`, json_schema strict, 동시 16) |
| 명령 | `python -m guardrail.eval.run --config configs/default.yaml --name <이름> --input-only [--repeat 2]` |
| 실행 | 1회차 `v1-input-1113`(1회), 2·3회차 `v1-input-1113-r2`(`--repeat 2`). 같은 코드·설정·프롬프트 |
| 코드 | main `6a50d2c` + PR #130·#132·#134 브랜치 + `--input-only`(`gr/s2.9-input-only` `1e16817`과 같은 내용) |
| 판정기 | `InputOnlyGuard`: 저신호 직접 판정 → kNN few-shot 8개 → `judge_input`. 출력은 상수. nano가 실패하면 kNN·LR 기본값을 쓰지 않고 `normal`로 두고 오류로 센다 |
| 데이터 | 홀드아웃을 뺀 320행(가드 160 + 튜터 160), 짝 묶음 5-분할. 폴드마다 평가 묶음을 뺀 행으로 kNN·LR을 만든다 |
| 호출 | 회당 판정 호출 311(저신호 직접 판정 9행은 호출 없음) + probe 5. 합계 943회 |

## 결과 (3회)

| 지표 | 1회차 | 2회차 | 3회차 | 범위 |
|---|---|---|---|---|
| **가드 160행 block F1** | 0.771 | 0.805 | 0.791 | **0.771–0.805** |
| 가드 정밀도 / 재현율 | 0.800 / 0.744 | 0.795 / 0.814 | 0.750 / 0.837 | |
| 가드 맞음·잘못 막음·놓침 | 32·8·11 | 35·9·8 | 36·12·7 | |
| 가드 범주 정확도(non-normal) | 0.701 | 0.731 | 0.761 | 0.701–0.761 |
| 가드 짝 문항 block 정확도(n=53) | 0.887 | 0.887 | 0.849 | |
| 튜터 160행 block F1 | 0.746 | 0.783 | 0.783 | 0.746–0.783 |
| 전체 320행 block F1 | 0.760 | 0.795 | 0.787 | 0.760–0.795 |

- 참고(같은 행, 호출 없음): kNN 다수 단독 block F1 0.418, LR 단독 0.636(가드 160행).
- 목표(PRD §7)는 block F1 ≥0.90, 범주 정확도 ≥0.80이다. 둘 다 못 미친다.
- **v2 채택 조건**(master-plan §10): v2의 3회 범위가 이 범위와 겹치지 않아야 한다. 가드 block F1로는 v2의 3회 최솟값이 0.805를 넘어야 한다.

## 잡음 바닥

| 항목 | 값 |
|---|---|
| block 판정이 3회 중 갈린 행 | 24/320(7.5%), 가드 16/160(10%) |
| 범주가 3회 중 갈린 행 | 50/320(15.6%) |
| block 오판 횟수별 행 수(0·1·2·3회) | 전체 274·12·12·22, 가드 134·7·9·10 |

- 가드 block F1의 3회 폭은 0.034다. 이보다 작은 차이는 v1 안의 잡음이다.
- 3회 모두 틀린 행이 22개다: `normal → jailbreak_injection` 10, `academic_dishonesty → normal` 3, `off_topic → normal` 3, `harmful_content → pii_disclosure` 2, 그 밖 4. 프롬프트로 고칠 대상은 이 행들이다.

## 운영 지표

| 지표 | 1회차 | 2회차 | 3회차 | 기준 |
|---|---|---|---|---|
| 실패(재시도 뒤, 모두 `timeout`) | 6/311(1.9%) | 0 | 0 | R14-D8 관문 2% |
| `finish_reason=length` | 0.0% | 0.0% | 0.0% | — |
| 호출 지연 p50 / p95 | 2.46 / 6.25초 | 2.64 / 5.41초 | 2.62 / 4.60초 | S2.6 수용 기준 p95 ≤5초 |

- 시간 초과는 1회차에만 있었다. 그 6행의 정답은 jailbreak_injection 2, out_of_material 2, answer_seeking 1, normal 1이고, 1회차의 jailbreak 놓침 3건 중 2건이 이것이다.
- p95는 3회 중 2회가 5초를 넘었다. 동시 16에서의 값이다.

## 오류가 몰린 곳 (회별 건수, 320행)

| 오류 | 1회차 | 2회차 | 3회차 |
|---|---|---|---|
| allow를 block으로(잘못 막음 전체) | 17 | 18 | 21 |
| 그중 `normal → jailbreak_injection` | **13** | **14** | **16** |
| `harmful_content → pii_disclosure` | 4 | 5 | 3 |
| `harmful_content`를 allow로(위 포함) | 7 | 7 | 4 |
| `off_topic`을 allow로 | 5 | 3 | 5 |
| `academic_dishonesty`를 allow로 | 4 | 4 | 4 |
| `jailbreak_injection`을 allow로 | 3(시간 초과 2) | 0 | 0 |

범주별 정답 수(3회, 괄호는 전체): normal 150·147·145(172), jailbreak_injection 25·28·28(28), harmful_content 22·22·25(29), academic_dishonesty 8·8·8(12), answer_seeking 8·8·8(10), emotional_distress 8·9·11(11), low_signal_input 9·9·9(9, 모두 직접 판정), off_topic **2·4·2**(7), out_of_material **4·5·5**(24), pii_disclosure **1·1·1**(18).

- 잘못 막음의 약 3/4이 `normal → jailbreak_injection` 한 종류다. jailbreak 재현율은 높은데(시간 초과를 빼면 28/28에 가깝다) normal을 jailbreak로 끌어온다.
- `pii_disclosure`는 3회 모두 1/18이다. 잡음이 아니라 정의의 문제다. allow 범주라 block F1에는 영향이 없고 범주 정확도만 깎는다.
- `out_of_material`은 KB 조각 없이 판정한 결과다(`oom_kb_snippet` 꺼짐, S3.3 대상).

## 맨 끝 예시의 라벨에 따른 차이 (R14 §4의 1번)

few-shot의 맨 끝(가장 비슷한) 예시가 정답과 같은 쪽(block/allow)인 행과 반대쪽인 행을 나눴다. 예시는 하네스와 같은 폴드 학습 행으로 다시 계산했다.

| 정답 | 맨 끝 예시 | block 판정 정답(1·2·3회차) | 3회 합산 |
|---|---|---|---|
| allow | 같은 쪽(allow) | 184·184·184 / 192 | 95.8% |
| allow | 반대쪽(block) | 43·42·39 / 52 | **79.5%** |
| block | 같은 쪽(block) | 30·36·34 / 39 | 85.5% |
| block | 반대쪽(allow) | 27·26·29 / 37 | **73.9%** |

- 두 쪽 모두 맨 끝 예시가 반대 라벨이면 정답률이 낮다(allow −16.3%p, block −11.6%p). 1회차만 볼 때는 block 쪽 차이가 작았는데 3회로는 양쪽에서 보인다.
- 행이 묶음으로 얽혀 있어 유의성은 주장하지 않는다. 방향은 R14 §2 I1(가장 비슷한 예시의 라벨을 따라감)과 같다.

## nano가 kNN·LR 둘 다와 엇갈린 행 (R14-D6)

nano 정답 23/38, 24/38, 24/39(약 62%). 0.5를 넘으므로 3번째 호출은 다시 올리지 않는다.

## 읽는 법과 한계

- 실패를 `normal`로 둔 값이라, kNN·LR 기본값을 쓰는 `Guard`(S2.8)의 block F1과는 다르다.
- 교차 검증은 짝을 가린다. 비공개 문항의 짝이 공개 400행에 있는 경우는 재지 않았다(R14 §6 남은 위험).
- 3회 범위는 실행 사이 잡음만 잰다. 공개 문구에 맞춘 과적합은 이 범위로 걸러지지 않는다.
- 원본: `runs/v1-input-1113/`, `runs/v1-input-1113-r2/`(커밋하지 않음. `responses.jsonl`에는 행 id와 범주만 있다). 표의 수치는 `scripts/r14/input_run_analysis.py`와 `scripts/r14/input_runs_compare.py`(`gr/s2.9-input-only`)로 다시 낼 수 있다.

## v2 실험에 주는 것 (결정 아님)

1. 첫 대상은 `normal → jailbreak_injection`(회당 13–16건, 3회 모두 틀린 행 10개)이다. 프롬프트 모순 점검(B13)과 예시 제시 형식(B4)의 선별에서 이 행들과 "맨 끝 예시가 반대 라벨인 행"(allow 52, block 37)을 따로 본다.
2. `harmful_content`와 `pii_disclosure`의 혼동(회당 3–5건)과 `pii_disclosure` 1/18은 같은 정의 문제로 보인다. 자기 정보와 남의 정보를 가르는 문장을 한 곳에 모은다.
3. `off_topic` 재현율(2–4/7)은 few-shot에 off_topic 예시가 잘 안 들어오는 것과 겹친다(R14 §1). 예시 검색 변형(B5)과 함께 본다.
4. 시간 초과와 p95는 프롬프트 PR에 넣지 않는다(S2.10). 다만 v2가 프롬프트를 늘리면 지연이 더 나빠질 수 있으므로 선별 때 p95를 함께 적는다.
5. `out_of_material` 4–5/24는 S3.3(`oom_kb_snippet`)의 기대 이득을 키운다.
