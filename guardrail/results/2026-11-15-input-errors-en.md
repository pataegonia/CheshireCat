# 감점 위치 분석: 영어판 1.1 교차 검증 3회 (호출 0)

작성 2026-11-15 · 담당 danielcho02 · 이슈 #152(S3.2) · 이전 분석: [2026-11-13-error-analysis.md](2026-11-13-error-analysis.md)(한국어 v1, 유효 6회)

v1 제출(2026-11-15, 가드 89.71)은 채점 메시지가 총점만 주므로, 같은 프롬프트(영어 1.1)로 잰 교차 검증 3회(`v1en-en-r1`–`r3`, [2026-11-14-v1-english.md](2026-11-14-v1-english.md))의 저장된 응답을 다시 셌다. 홀드아웃 묶음은 세지 않았고 API 호출은 없다. 도구는 `scripts/r14/error_breakdown.py`. 문항 원문은 여기 옮기지 않는다.

스크립트가 다시 계산한 가드 점수가 실행 보고서와 같다(r2 88.33, r3 84.87).

## 잃는 점수 (가드 160행, 출력 64행)

| 항 | r1 · r2 · r3 | 잃는 점수(평균) |
|---|---|---|
| block F1 (45점) | 0.813 · 0.867 · 0.810 | 7.7 |
| 범주 정확도 (10점) | 0.761 · 0.791 · 0.716 | 2.4 |
| 출력 평균 (45점) | 0.909 · 0.920 · 0.917 | 3.8 = 구간 F1 2.6 + grounded 0.6 + safe 0.5 |

## 입력 쪽 오류 (회당 평균 행 수)

| 유형 | 회당 | 11/13(한국어 v1) |
|---|---|---|
| 오탐 normal → jailbreak_injection | 7.0 | 7.5 |
| 오탐 normal → off_topic | 1.0 | |
| 미탐 harmful_content → pii_disclosure | 1.7 | 4.2(harmful → pii·normal 합) |
| 미탐 harmful_content → normal | 1.0 | |
| 미탐 off_topic → normal | 1.7 | 2.5(academic·off_topic 합) |
| 미탐 academic_dishonesty → normal | 1.3 | |
| 미탐 academic_dishonesty → answer_seeking | 0.7 | |
| 범주만 out_of_material → normal | 4.0 | 4.2 |
| 범주만 pii_disclosure → answer_seeking | 2.3 | 4.3(pii → normal) |
| 범주만 pii_disclosure → normal | 2.0 | |
| 그 밖(각 0.3) | 1.7 | |

- block 판정을 3회 모두 틀린 행 10개(그중 normal → jailbreak 5개), 일부 회차만 틀린 행 12개.
- 3회 모두 틀린 normal → jailbreak 행의 모양: 탈옥·주입 문구를 **예로 보여 달라, 검토해 달라, 방어법을 묻는다, 인용해 분석해 달라**, 또는 "거절하지 말고"와 형식 바꾸기 요청. 공통으로 "무시·제한 없는·숨겨진 지시·거절하지 말고" 같은 촉발어가 들어 있다. 지금 프롬프트에 "튜터에게 적용 = 탈옥, 다루는 대상 = normal" 규칙이 이미 있는데도 틀린다.
- 놓친 행의 모양: 남의 이름·학교를 알아봐 달라(→ pii_disclosure), 튜터를 향한 모욕(→ normal), 신분을 주장하며 완성 코드를 요구(→ normal), 과정과 무관한 그림(→ normal).

## 출력 쪽 (회당 평균 행 수)

| 유형 | 회당 |
|---|---|
| 구간 F1 < 1, ③단(nano 인용) | 13.3 |
| 구간 F1 < 1, ②단(숫자·리터럴 규칙) | 12.0 |
| safe 틀림(정답 true) | 1.7 |
| grounded 틀림 ④단(정답 false) | 1.7 |
| grounded 틀림 ③단(정답 true) | 0.7 |
| safe 틀림(정답 false) | 0.7 |
| grounded 틀림 ①단(정답 false) | 0.3 |

출력 손실의 대부분은 근거 없음을 맞게 잡고도 구간 경계가 정답과 어긋나는 데서 난다(S3.1 영역).

## 다음

입력 판정 프롬프트 v2(S3.2): 경계별 판별 가이드(V4)를 폴드 0–2 오판만 보고 쓰고 폴드 3–4에서 일반화를 확인한다. 선행 조사는 `docs/research/R15-boundary-prompting.md`.
