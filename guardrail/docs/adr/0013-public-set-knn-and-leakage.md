# ADR-0013: 공개 세트 kNN few-shot과 합성 데이터, 누수 방지

**Date**: 2026-11-10
**Status**: proposed
**Deciders**: danielcho02 (council 2026-11-10, [R13 §2](../research/R13-design-research.md#2-council-가드-v6-구조))
**PRD**: GR-04, GR-05, GR-11, GR-13, GR-14
**대체**: ADR-0002 v5의 "학습 분류기 불가"

## Context

SPEC §7.1은 세 가지를 명시적으로 허용한다. 공개 세트를 few-shot 예시로 쓰는 것, 질문과 비슷한 공개 세트 예시를 유사도 검색으로 찾아 넣는 것, 배포 자료와 운영진 제공 LLM으로 만든 예시로 처음부터 학습한 분류기다. 다른 LLM이나 본인 키로 만든 예시 묶음, 인터넷 데이터, 미리 학습된 모델 파일은 금지다. 받은 채점 요청으로 예시·규칙을 만들거나 유사도 캐시를 두면 실격이다(SPEC §8).
공개 세트는 같은 문장 틀에서 1–5어절을 바꿔 범주를 뒤집는 짝으로 만들어졌다(비normal의 35%, [R13-A §1.1](../research/R13-public-set-analysis.md#11-최소쌍-근접-중복)). `tutor.jsonl`도 같은 10범주 라벨을 가져 분류 자료가 400행이 된다([R13 §1 M3](../research/R13-design-research.md#1-근거-정리-research-ops-형식)). 유사 예시는 무작위보다 낫지만, 예시를 늘린다고 꾸준히 좋아지지는 않는다(S4, [R13-B §2-1](../research/R13-B-external-research.md#2-1-예시-선택-knn유사도-vs-무작위)).

## Decision

**예시 풀과 색인 (D3)**
- 풀은 `public/guard.jsonl` 200행 + `public/tutor.jsonl` 200행이다. 서버 기동 때 이 두 파일만 읽어 메모리에 색인한다(SPEC §3). 요청을 처리하는 동안 색인에 아무것도 더하지 않고 디스크에 쓰지 않는다(SPEC §8).
- 색인 대상은 각 행의 마지막 학습자 발화(정규화본)이고, 예시는 발화 + 범주만 넣는다. 유사도는 문자 n-gram TF-IDF 코사인이다(scikit-learn `TfidfVectorizer(analyzer="char_wb")`, [R13-B2 §3-3](../research/R13-B2-libraries.md#3-3-분류기)).
- 고르는 규칙([R13 D3](../research/R13-design-research.md#3-결정-d1d10)): 4–8개. 규칙끼리 부딪치면 이 순서로 따른다. ① block/allow 반대쪽 최근접 예시 1개는 반드시 넣는다 ② 범주당 최대 2개 ③ 전체 최대 8개 ④ 고른 예시가 짝 묶음에 속하면 그 짝은 ②·③을 지킬 때만 더한다(santa 4라운드, PRD GR-05). 묶음은 `KnnIndex`가 기동 때 서버 쪽 모듈(`guardrail/clusters.py`)의 공용 함수로 직접 계산한다. `eval.cv`도 같은 함수를 import하고, 서버는 eval 패키지에 의존하지 않는다. 가장 비슷한 예시를 마지막(질의 바로 앞)에 둔다([R13-B §2-2](../research/R13-B-external-research.md#2-2-라벨-균형순서개수)). 개수는 이 범위 안에서 교차 검증으로 고른다.
- 교란이 붙은 normal 예시(거절하지 말고·소설 틀 등)가 이웃이면 그대로 넣는다. 과잉 차단을 막는 예시다([R13-A 설계 시사점 2](../research/R13-public-set-analysis.md#설계-시사점)).
- 임베딩 kNN(`text-embedding-3-small`, 400행 임베딩은 기동 때 계산)은 ablation이다. 요청마다 임베딩 호출이 하나 는다.

**보조 판정기 (GR-05)**
- 같은 400행으로 기동 때 문자 n-gram(1–3) TF-IDF + LR을 학습한다. 저장한 pickle은 로드하지 않는다. kNN 이웃의 다수결도 함께 낸다.
- 보조 판정기는 **nano를 건너뛰는 데 쓰지 않는다**(ADR-0012). 쓰임은 셋이다(PRD GR-05).
  1. few-shot 예시 고르기(위).
  2. 입력 nano 실패 기본값(D8): kNN 다수 범주와 LR 범주가 같으면 그 범주, 다르면 LR 범주. LR이나 kNN이 꺼져 있으면 있는 쪽을 쓰고, 둘 다 없으면 `normal`(ADR-0009 v6). kNN 다수결은 이웃 `input.knn_vote_k`개(기본 7)로 낸다.
  3. 조건부 재호출(D1, ablation, 기본 꺼짐): nano의 block 여부가 kNN 다수와 LR **둘 다**와 엇갈리면 입력 판정을 1회 더 부르고, 세 판정(nano 2회 + kNN·LR 합의)의 다수로 block 여부를 정한다. 최종 범주는 nano 2번째 호출의 범주다. 2번째 호출이 실패하거나 시간을 넘기면 1번째 호출의 범주를 그대로 둔다(ADR-0011 v6).
- 보조 판정 결과를 프롬프트 힌트로 넣지 않는다. nano가 힌트를 그대로 따라 오류를 복제할 위험이 있다([R13-B §2-6](../research/R13-B-external-research.md#2-6-가벼운-학습-분류기와-결합)).

**합성 데이터 (D4)**
- 기본으로 쓰지 않는다. tutor 200행이 적은 범주(answer_seeking·emotional_distress·low_signal_input·off_topic)를 이미 늘린다([R13-A 설계 시사점 4](../research/R13-public-set-analysis.md#설계-시사점)).
- 쓴다면 운영진 제공 `gpt-5-nano`로만 만든다. 다른 LLM이나 본인 키로 만든 묶음은 외부 데이터다(SPEC §7.1). 용도는 짝 대조 예시와 거절 정규식 검증(ADR-0014)뿐이다.
- kNN 풀에는 실제 공개 예시만 둔다([R13-B §5-1](../research/R13-B-external-research.md#5-1-분류용-합성-데이터)). 평가 분할에는 섞지 않는다. 생성 스크립트·모델을 `provenance.md`에 적는다.

**누수 방지**
- 평가는 짝 묶음 단위로 뺀다(leave-one-cluster-out 또는 묶음 단위 5-분할, ADR-0006 v6). 평가할 행의 묶음 전체를 kNN 색인과 LR 학습에서 빼고 돌린다. 홀드아웃(가드 묶음 단위 20%)은 색인·학습에서도 뺀 채로 최대 3회만 본다.
- 합성 예시는 학습 쪽 묶음에서만 만들고, 평가 행과 근접 중복이면 버린다([R13-B §5-3](../research/R13-B-external-research.md#5-3-누수-없는-평가)).
- 서버 코드에는 요청 텍스트로 색인·분류기를 갱신하는 경로가 없어야 한다. 시연 때 확인하는 항목이다(SPEC §8).

## Alternatives Considered

### 팀이 쓴 고정 few-shot (v5)
- **Pros**: 예시가 늘 같아 재현이 쉽다.
- **Cons**: 경계는 문항마다 다르다. 고정 예시로는 질의와 가까운 짝을 보여 줄 수 없다.
- **Why not**: SPEC §7.1이 공개 세트 유사도 검색을 허용하고, 유사 예시가 고정·무작위보다 낫다.

### 임베딩 kNN을 기본으로
- **Pros**: 바꿔 말한 표현에 강하다.
- **Cons**: 요청마다 임베딩 호출이 늘고, 이득이 측정되지 않았다.
- **Why not**: 문자 n-gram은 호출이 없고 음절 변형에 강하다. 순이득이 측정되면 켠다.

### 합성 데이터로 적은 범주를 늘려 kNN 풀·분류기에 넣기 (R13-B §5-1 권고 일부)
- **Pros**: 실데이터 100개 수준에서는 합성 증강 이득이 크다는 보고가 있다.
- **Cons**: 같은 nano가 만든 예시를 같은 nano가 판정해 오류가 순환한다. 공개 세트의 짝 생성 방식과 분포가 다르다.
- **Why not**: Pragmatist·Skeptic: tutor 200행이 먼저다. 합성은 검증 용도로만.

### 예시를 10개 이상
- **Why not**: 입력 토큰과 지연이 늘고, 예시 수의 효과는 모델마다 비단조였다([R13-B §2-2](../research/R13-B-external-research.md#2-2-라벨-균형순서개수)). "추론 모델은 예시가 늘수록 좋아지지 않는다"는 **미확인**이다(인용 연구에 추론 모델이 없다, [verification.md §R13 #6](../research/verification.md#r13-2026-11-10)).

## Consequences

### Positive
- 질의와 가장 가까운 짝 대조를 nano가 직접 본다. 최소쌍 경계에 맞는 구조다.
- 입력 nano가 실패해도 범주가 상수 `normal`보다 낫다.
- 쓰는 자료가 SPEC §7.1의 허용 문장과 하나씩 맞는다.

### Negative
- 기동 시간이 는다(TF-IDF·LR 학습, 수백 ms 추정). scikit-learn·scipy가 런타임 의존성이 된다([R13-B2 §3-3](../research/R13-B2-libraries.md#3-3-분류기)).
- 비공개 문항이 공개 세트 틀과 다르면 이웃이 엉뚱해진다.

### Risks
- 공개 세트 과적합: 설정은 묶음 교차 검증으로만 고르고, 행 단위 점수는 보고하지 않는다.
- 이웃이 normal로 쏠린다(guard 114/200): 범주당 상한과 반대쪽 최근접 필수 규칙으로 막는다.
- 합성 데이터 출처 위반: 기본 꺼짐. 켜면 생성 키·모델·스크립트를 기록한다.
