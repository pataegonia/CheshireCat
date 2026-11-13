# 입력 프롬프트 v1 단독 실측 (R14-D1)

작성 2026-11-13 · 담당 danielcho02 · 근거 계획: [R14 §6](../docs/research/R14-prompt-uplift.md#6-council-2026-11-13-후보-선별) R14-D1

**1회 실행이라 분산을 재지 않았다. 아래 수치는 방향만 본다.** 채택·비교에 쓰지 않는다.

## 실행

| 항목 | 값 |
|---|---|
| 어디서 | 런박스(`openai/gpt-5-nano`, `reasoning_effort: low`, json_schema strict, 동시 16) |
| 명령 | `python -m guardrail.eval.run --config configs/default.yaml --name v1-input-1113 --input-only` |
| 코드 | main `6a50d2c` + PR #130·#132·#134 브랜치 + `--input-only` 패치(커밋 전). 프롬프트는 `input_judge.md` version 1 그대로 |
| 판정기 | `InputOnlyGuard`: 저신호 직접 판정 → kNN few-shot 8개 → `judge_input`. 출력은 상수. nano가 실패하면 kNN·LR 기본값을 쓰지 않고 `normal`로 두고 오류로 센다 |
| 데이터 | 홀드아웃을 뺀 320행(가드 160 + 튜터 160), 짝 묶음 5-분할. 폴드마다 평가 묶음을 뺀 행으로 kNN·LR을 만든다 |
| 호출 | 예상 325(probe 5 + 320), 실제 판정 호출 311(저신호 직접 판정 9행은 호출 없음). 3분 18초 |

## 결과

| 지표 | 가드 160행 | 튜터 160행 | 전체 320행 |
|---|---|---|---|
| block F1 | **0.771** | 0.746 | 0.760 |
| 정밀도 / 재현율 | 0.800 / 0.744 | 0.735 / 0.758 | 0.770 / 0.750 |
| 맞음·잘못 막음·놓침 | 32 · 8 · 11 | 25 · 9 · 8 | 57 · 17 · 19 |
| 범주 정확도(non-normal) | 0.701 | 0.494 | 87/148 |
| 짝 문항 block 정확도 | 0.887 (n=53) | — | — |

참고(같은 행, 호출 없음): kNN 다수 단독 block F1 0.418, LR 단독 0.636(가드 160행).

**운영 지표**

| 지표 | 값 | 기준 |
|---|---|---|
| 실패(재시도 뒤) | 6/311 = 1.9%, 모두 `timeout` | R14-D8 관문 2%에 거의 닿음. S2.6 수용 기준 성공률 ≥98%는 98.1%로 턱걸이 |
| `finish_reason=length` | 0.0% | — |
| 호출 지연 | p50 2.46초, p95 6.25초 | S2.6 수용 기준 p95 ≤5초를 **넘음**(동시 16에서) |

실패한 6행의 정답은 jailbreak_injection 2, out_of_material 2, answer_seeking 1, normal 1이다. 놓친 jailbreak 3건 중 2건이 이 시간 초과다(nano가 답한 jailbreak 26건 중 25건은 맞았다).

## 오류가 몰린 곳

**잘못 막음 17건 가운데 14건이 `normal → jailbreak_injection`이다**(가드 6, 튜터 8). 나머지는 `normal → off_topic` 2, `normal → harmful_content` 1. 이것이 가장 큰 단일 오류다.

놓침 19건: `off_topic → normal` 5, `harmful_content → pii_disclosure` 4, `academic_dishonesty → normal` 4, `harmful_content → normal` 3, `jailbreak_injection → normal` 3(그중 2건은 시간 초과).

범주별 정답 수(320행):

| 범주 | 정답/전체 | 범주 | 정답/전체 |
|---|---|---|---|
| normal | 150/172 | out_of_material | **4/24** |
| jailbreak_injection | 25/28 | pii_disclosure | **1/18** |
| harmful_content | 22/29 | off_topic | **2/7** |
| academic_dishonesty | 8/12 | answer_seeking | 8/10 |
| emotional_distress | 8/11 | low_signal_input | 9/9(모두 직접 판정) |

- `pii_disclosure` 1/18: 대부분 `normal`로 갔다. allow 범주라 block F1에는 영향이 없고 범주 정확도만 깎는다.
- `out_of_material` 4/24: KB 조각 없이 판정한 결과다(`oom_kb_snippet` 꺼짐, S3.3 대상).
- `off_topic` 2/7: few-shot에 off_topic 예시가 들어오는 비율이 낮았던 범주다(R14 §1, 2/7).

## 맨 끝 예시의 라벨에 따른 차이 (R14 §4의 1번)

few-shot의 맨 끝(가장 비슷한) 예시가 정답과 같은 쪽(block/allow)인 행과 반대쪽인 행을 나눴다. 예시는 하네스와 같은 폴드 학습 행으로 다시 계산했다(`scripts/r14/input_run_analysis.py`).

| 정답 | 맨 끝 예시 | block 판정 정답 |
|---|---|---|
| allow | 같은 쪽(allow) | 184/192 (95.8%) |
| allow | 반대쪽(block) | 43/52 (**82.7%**) |
| block | 같은 쪽(block) | 30/39 (76.9%) |
| block | 반대쪽(allow) | 27/37 (73.0%) |

- allow 행에서는 차이가 크다: 맨 끝 예시가 block이면 잘못 막는 비율이 17.3%, allow면 4.2%다. 잘못 막음 17건 중 9건이 "맨 끝 예시가 block"인 52행에서 나왔다.
- block 행에서는 차이가 작다(76.9% 대 73.0%, 행 수 39·37).
- 행이 묶음으로 얽혀 있고 1회 실행이라 유의성은 주장하지 않는다. 방향은 R14 §2 I1(가장 비슷한 예시의 라벨을 따라감)과 같되, 드러난 곳은 "놓침"이 아니라 "잘못 막음" 쪽이다.

## nano가 kNN·LR 둘 다와 엇갈린 행 (R14-D6)

38행, 그중 nano가 맞은 것이 23행(60.5%)이다. 0.5를 넘으므로 R14-D6대로 3번째 호출은 다시 올리지 않는다.

## 읽는 법과 한계

- 1회 실행이다. 같은 프롬프트의 실행 사이 뒤집힘(잡음 바닥)은 S2.9의 3회 실측에서 잰다.
- 가드 행 160개 기준 block 오판 1건은 F1 약 0.01–0.015다(잘못 막음 1건 0.009, 놓침 1건 0.015).
- 실패를 `normal`로 둔 값이라, kNN·LR 기본값을 쓰는 `Guard`(S2.8)의 block F1과는 다르다.
- 교차 검증은 짝을 가린다. 비공개 문항의 짝이 공개 400행에 있는 경우는 재지 않았다(R14 §6 남은 위험).
- 원본: `runs/v1-input-1113/`(커밋하지 않음. `responses.jsonl`에는 행 id와 범주만 있다).

## 다음 실험에 주는 것 (결정 아님)

1. `normal → jailbreak_injection` 14건이 첫 대상이다. 프롬프트 모순 점검(B13)과 예시 제시 형식(B4)의 선별 실험에서 이 14건과 "맨 끝 예시가 block인 allow 행 52개"를 따로 본다.
2. 시간 초과 1.9%와 p95 6.25초는 동시 16에서의 값이다. 동시 64 부하 시험(S2.10) 전에 `read` 시간 제한(12초)과 재시도 대기를 다시 본다. 관문(2%)을 넘으면 문구보다 길이를 먼저 줄인다(R14-D8).
3. `pii_disclosure` 1/18은 정의 한 줄의 문제로 보인다(자기 정보를 밝혔으면 학습 질문이 섞여도 해당). 범주 정확도 항(0.10)에만 걸리므로 block 경계 다음 순서다.
4. `out_of_material` 4/24는 S3.3(`oom_kb_snippet`)의 기대 이득을 키운다.
