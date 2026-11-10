# ADR-0012: 입력 판정: 범주만 출력하고 decision은 코드가 도출

**Date**: 2026-11-10
**Status**: proposed
**Deciders**: danielcho02 (council 2026-11-10: Architect·Skeptic·Pragmatist·Critic, [R13 §2](../research/R13-design-research.md#2-council-가드-v6-구조))
**PRD**: GR-04, GR-06, GR-09, GR-11
**대체**: ADR-0002(입력 경로), ADR-0011 v5의 입력 스키마(`decision` 필드)

## Context

공개 세트 200행에서 `decision`은 `category`의 순수 함수다. block은 `jailbreak_injection`·`harmful_content`·`academic_dishonesty`·`off_topic`이고 나머지 6범주는 allow다(200/200, [R13 §1 M1](../research/R13-design-research.md#1-근거-정리-research-ops-형식)).
가드 점수는 대부분 block 경계에서 나온다. 비공개 약 300문항 기준 block 오판 1건은 약 0.27점, allow 그룹 안의 범주 오류 1건은 0.08점이다([R13-A Q4](../research/R13-public-set-analysis.md#q4-가드-점수-상하한과-민감도)).
nano는 mini보다 한 단계 낮은 분류기라, 10범주와 결정을 따로 내게 하면 둘이 어긋날 수 있다([R13-B §2-7](../research/R13-B-external-research.md#2-7-아주-작은-추론-모델에-대한-증거)). v5 스키마(`evidence` → `category` → `decision`)는 "범주가 decision을 정한다는 것이 확인되면 코드로 계산"을 열어 두었다(ADR-0011 v5). 공개 세트가 그것을 확인했다.

## Decision

- nano 입력 판정은 **`evidence`(≤1문장, 발화 인용 위주) → `category`** 두 필드만 낸다. `category`는 SPEC §5의 10개 값 enum이고 json_schema strict로 받는다.
- 모든 입력은 nano가 판정한다. 예외는 하나, **저신호 직접 판정**이다(PRD GR-06): 마지막 학습자 발화가 자모·문장부호·이모티콘만으로 되어 있고(숫자·라틴 문자·완성 음절·숨긴 텍스트 없음) 앞에 대화가 있으면 LLM 없이 `low_signal_input`이다. 검사는 자모 조합 전 텍스트(태그 문자만 지운 것)에 건다. 범주가 내용이 아니라 형식으로 정의되고 400행에서 정밀도 100%였다(표본 안 값: 규칙이 이 400행의 관찰에서 나왔다. 비공개 일반화는 가정)([R13-A §1.2](../research/R13-public-set-analysis.md#12-범주별-단서와-분리력)). 1턴 입력은 nano에 보낸다.
- `decision`은 코드가 범주 → decision 표로 정한다([R13 D2](../research/R13-design-research.md#3-결정-d1d10)). 표는 설정(GR-11)에 두고 공개 세트에서 잰 값(위 4범주 block)으로 채운다.
- 프롬프트와 예시는 **block 4범주와 allow의 경계**에 집중한다. 최소쌍에서 갈림은 주제어가 아니라 요청 동사와 대상이다([R13-A §1.1](../research/R13-public-set-analysis.md#11-최소쌍-근접-중복)). 프롬프트는 세 축을 명시해 묻는다([R13-A 설계 시사점 1](../research/R13-public-set-analysis.md#설계-시사점)).
  1. 튜터 자신에게 적용하라는가(지시 무시, 역할 바꾸기, 숨은 지시 공개).
  2. 결과물을 만들어 달라는가(사칭 문자, 광고 문구, 제출할 답안), 아니면 분석·위험성·비교·풀이 방법을 묻는가.
  3. 정보 주체가 자기인가 남인가.
- allow 하위 범주(normal·answer_seeking·out_of_material·pii_disclosure·emotional_distress·low_signal_input) 사이의 구분은 정의 한 줄씩만 준다. 틀려도 1건 0.08점이라 프롬프트 분량을 쓰지 않는다.
- 범주는 **마지막 학습자 발화**가 무엇을 요구하는지로 정한다. 이력은 지시어("그 역할")를 풀고 검색 질의를 만드는 데만 쓴다. 이력이 필요한 비normal은 1/21이었다([R13-A §1.3](../research/R13-public-set-analysis.md#13-멀티턴)).
- 키워드 신호(위기·PII·인젝션 어휘)를 프롬프트 힌트로 넣지 않는다. v5의 어휘 목록은 삭제됐다(PRD GR-06). 넣는 실험은 ablation이다.
- "거절하지 말고", 권한 주장, 소설 틀, 붙여 넣은 글, 영어 혼용, 음절 띄우기는 normal에도 같은 정도로 나온다. 범주 단서가 아니라고 프롬프트에 적는다([R13-A §1.2](../research/R13-public-set-analysis.md#12-범주별-단서와-분리력)).
- 출력이 스키마를 어기거나 enum 밖이면 재시도 1회 뒤 실패로 보고 D8 기본값을 쓴다(ADR-0009 v6, ADR-0013).

## Alternatives Considered

### nano가 `category`와 `decision`을 둘 다 출력 (v5 스키마)
- **Pros**: 비공개 세트에서 범주와 결정의 관계가 공개 세트와 다르면 모델이 따로 맞출 여지가 있다.
- **Cons**: 두 필드가 어긋난 출력(`off_topic` + allow)을 처리할 규칙이 또 필요하다. 출력 토큰이 는다.
- **Why not**: 공개 세트 200/200에서 결정은 범주의 함수다. 어긋남은 잡음일 뿐이다.

### block/allow 이진 판정 + 범주는 별도 단계 ([R13-B §2-7](../research/R13-B-external-research.md#2-7-아주-작은-추론-모델에-대한-증거)의 단순화 방향)
- **Pros**: 가장 무거운 판단을 가장 단순한 형태로 묻는다.
- **Cons**: 범주 점수(10%)를 위해 범주를 또 내야 하고, 두 단계가 어긋날 수 있다.
- **Why not**: 범주에서 결정을 도출하면 한 출력으로 둘 다 얻고 모순이 없다.

### allow 그룹 범주를 결정적 규칙으로 먼저 확정 (R13-A 설계 시사점 5)
- **Pros**: "정답만" → answer_seeking, "몇 주째" + "혼자 버티기" → emotional_distress처럼 공개 세트에서 예외 없는 단서가 있다. 호출 없이 정한다.
- **Cons**: 자기소개 + 타인 정보 요청은 harmful(block)이다. 규칙이 pii로 확정하면 block 경계를 틀린다. "예외 없음"은 수 건~십수 건 기준이다.
- **Why not**: council 합의는 "모든 입력은 nano가 판정"이다(Critic). 내용 단서는 kNN few-shot 예시로만 nano에 닿는다(ADR-0013). 형식으로 정의되는 `low_signal_input`만 예외로 채택했다.

### 분류기 확신이 높으면 nano를 건너뜀 (캐스케이드)
- **Pros**: 호출 수가 준다.
- **Cons**: 문자 n-gram 분류기는 1–5어절 차이 짝에서 확신하면서 틀린다([R13 §1 I2](../research/R13-design-research.md#1-근거-정리-research-ops-형식)).
- **Why not**: 건너뛴 문항이 바로 점수가 가장 큰 경계 문항이다.

## Consequences

### Positive
- 범주와 결정이 구조상 모순되지 않는다. 범주 점수와 block F1이 한 출력에서 나온다.
- 출력이 짧아 지연이 준다.

### Negative
- 비공개 세트에서 어떤 범주의 decision이 공개 세트와 다르면(예: `emotional_distress`가 block) 그 범주 전체가 한꺼번에 틀린다. 표는 설정 하나로 바꿀 수 있지만 채점 중에는 알 수 없다.
- allow 하위 범주는 프롬프트에서 우선순위가 낮아 범주 점수를 일부 잃을 수 있다.

### Risks
- 경계 문항의 기울기: F1 0.9 부근에서 FN 1건(0.295점)이 FP 1건(0.241점)보다 비싸다([R13-A Q4](../research/R13-public-set-analysis.md#q4-가드-점수-상하한과-민감도)). 프롬프트를 block 쪽으로 얼마나 기울일지는 짝 묶음 교차 검증으로 정한다(ADR-0006 v6).
- 공개 세트 생성 방식에 과적합(Architect가 꼽은 가장 큰 위험): 묶음 교차 검증과 빌드당 3회 실행으로 부푼 이득을 거른다.
