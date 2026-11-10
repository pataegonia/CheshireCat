# ADR-0011: LLM 호출 구조 (입력·출력 2회 병렬, gpt-5-nano)

**Date**: 2026-11-09
**Status**: proposed
**Deciders**: danielcho02 (council 2026-11-09: Architect·Skeptic·Pragmatist·Critic)
**PRD**: GR-04, GR-05, GR-07, GR-10 (v5 번호: GR-04, GR-05, GR-07, GR-09)
**대체**: ADR-0004, ADR-0007(일부), ADR-0008(입력 판정 부분)

## v6 수정 (2026-11-10, R13 council)

이 블록이 본문보다 우선한다(본문이 v5다). PRD v6 GR-04·GR-05·GR-07·GR-10 기준.

**호출 구조 (D1)**
- 요청당 nano **2회 병렬**: 입력 범주(ADR-0012) / 출력 근거 판정(ADR-0014). `candidate_answer`가 없거나 비었으면 출력 호출을 하지 않고, 저신호 직접 판정에 걸리면 입력 호출을 하지 않는다. v6 모드에서는 후보 답변이 비어 있지 않으면 출력 호출을 늘 한다(③ 사용 여부는 토글 `nano_tier3`, ADR-0014).
- 입력에만 **조건부 3번째 호출 1회 상한**: nano의 block 여부가 kNN 다수와 LR 둘 다와 엇갈리면 입력 판정을 1회 더 부르고 세 판정(nano 2회 + kNN·LR 합의)의 다수로 block 여부를 정한다. 최종 범주는 nano 2번째 호출의 범주다. 2번째 호출이 실패하거나 시간을 넘기면 1번째 호출의 범주를 그대로 둔다(ADR-0013). ablation 플래그, 기본 꺼짐(P2). 짝 묶음 교차 검증에서 순이득이 3회 분산 밖에서 0보다 클 때만 켠다. 근거는 분산 안의 차이를 개선으로 보지 않는 [R13 D10](../research/R13-design-research.md#3-결정-d1d10)의 잡음 규칙이고, [R13 D1](../research/R13-design-research.md#3-결정-d1d10)의 "순이득 > 0"을 이 기준으로 조였다. 짝 문항에서 정답을 뒤집을 위험이 있다([R13 §4](../research/R13-design-research.md#4-남은-위험과-확인할-것)).
- **출력 2단(D)·자기 일관성(E)은 쓰지 않는다**(Skeptic의 전제 점검, [R13 §2](../research/R13-design-research.md#2-council-가드-v6-구조)). 점수 대부분은 입력 block 경계에서 나온다(입력 완벽 + 상수 출력 84.5 대 출력 오라클 +9.4, [R13-A Q4](../research/R13-public-set-analysis.md#q4-가드-점수-상하한과-민감도)). 투표 이득은 강한 모델에서 작다([R13-B §2-5](../research/R13-B-external-research.md#2-5-자기-일관성투표-비용-대비-효과)). 본문의 "2표 병렬, 엇갈리면 1표" 실험 후보도 닫는다.
- 요청당 호출 상한: nano 2회 + 조건부 1회 + 호출마다 재시도 1회. 임베딩 0회(임베딩 ablation을 켜지 않는 한).

**모델 파라미터**
- `reasoning_effort="low"`, `verbosity="low"`. **`minimal`은 쓰지 않는다**(지연 ablation도 안 함). 쉬운 과제에서 정확도가 무너진다(ObviousBench minimal 37.5 대 low 91.0 (약한 근거, 단일 출처, verification.md #2), [R13-B §4-3](../research/R13-B-external-research.md#4-3-정확도-effort별), [R13 §1 S3](../research/R13-design-research.md#1-근거-정리-research-ops-형식)). `medium` + verbosity low는 nano에서 오히려 39% 느렸다([R13-B §4-2](../research/R13-B-external-research.md#4-2-지연처리량)).
- json_schema strict, `max_completion_tokens` ≥800, `temperature`·`max_tokens`·`logprobs` 미전송.
- **기동 시험 호출(`probe()`)**: `reasoning_effort`·`verbosity`·`max_completion_tokens`·`response_format` json_schema를 **하나씩 따로** 시험하고 거절된 것만 끈다(json_schema가 거절되면 json_object로 내린다). 모든 파라미터는 설정에서 `null`(보내지 않음)로 둘 수 있다. 결과는 기동 때 오류 코드로만 남긴다. `max_completion_tokens`는 S1.6에서 800과 1200–1600을 `finish_reason=length` 비율로 비교해 정한다. 프롬프트는 고정 부분을 앞에, 요청별 부분(예시·대화·답변)을 뒤에 둔다(프롬프트 캐시 접두 일치).

**시간 제한과 재시도 (D10)**
- SDK 기본값(시간 제한 600초, 재시도 2회, `Retry-After` 최대 120초를 그대로 따름)은 30초 제한을 깬다([R13 §1 S6](../research/R13-design-research.md#1-근거-정리-research-ops-형식), [R13-B2 §3-6](../research/R13-B2-libraries.md#3-6-llm-호출-asyncopenai--64-동시-요청당-30초)). `AsyncOpenAI`를 lifespan에서 하나 만들고 시간 제한을 직접 준다(연결 3초·읽기 12초 수준). **SDK 재시도는 0**이다.
- 재시도는 코드가 호출마다 1회만 한다. 대상은 연결 오류·타임아웃·408·409·429·5xx·스키마 불일치(잘못된 JSON 포함)다. 대기는 `retry-after-ms` → `retry-after` → 지터 0.2–0.5초 순이고, 대기 + 읽기 한도가 요청 예산(약 20초, ADR-0009 v6)의 마감을 넘으면 재시도하지 않고 기본값으로 간다. 401·403·400·422는 바로 실패다(master-plan §4.3).
- 본문의 "호출당 약 12초, 요청 마감 25초"는 이것으로 바뀐다.

**전역 LLM 게이트 (D10)**
- `/tutor`와 같은 키(팀 키 하나, SPEC §7.1)를 쓰고, 같은 프로세스에 두면(팀 선택, ADR-0009 v6) LLM 게이트(세마포어 + 지터 재시도)는 서버 전체에 **하나**다. 가드는 게이트를 통해서만 부르고, 튜터도 같은 게이트를 쓰게 제안한다(PRD §9 T4). 세마포어 크기는 런박스 실측(동시 64, 가드·튜터 합쳐 약 600요청)으로 정한다(SPEC §3). 본문의 AIMD는 실측에서 429가 잦을 때 다시 본다.

**판정 내용 변경**
- 입력: 스키마는 `evidence`(≤1문장) → `category`이고 `decision` 필드는 없다(ADR-0012). 결정적 신호 힌트 대신 kNN few-shot 예시를 넣는다(ADR-0013). 첫 학습자 발화 + 마지막 메시지 6개를 주되, 범주는 마지막 학습자 발화로 정하라고 지시한다([R13-A §1.3](../research/R13-public-set-analysis.md#13-멀티턴)).
- 출력: nano는 `reason` → `grounded`만 낸다. `safe`는 nano에서 빼고 규칙으로 정한다(D6). 본문의 "근거 없는 부분 + safe 두 필드, `grounded`는 코드로 계산"과 문장 번호 선택은 3단 출력으로 바뀐다(ADR-0014). 근거 자료는 ADR-0010 v6.

## Context

쓸 수 있는 채팅 모델은 `gpt-5-nano` 하나다. OpenAI 직접 호출에서 이 모델은 `temperature`(기본값 외)·`logprobs`·`max_tokens`를 거부한다는 보고가 있다(OT는 temperature를 "자유"라고 했으므로 대회 프록시는 기동 시 확인). 추론 수준 `minimal`은 판단 과제에서 정확도가 무너지고, `low`는 JSON 분류에서 p50 약 2.6초·p95 약 3.9초다. 여러 과제를 한 프롬프트에 쌓으면 nano는 15.5%p 떨어졌다(R12 §4, §5). 채점기는 최대 64개를 동시에 보내고, 과한 호출은 1회 경고 뒤 키 회수다. 키의 호출·크레딧 한도는 따로 없다(OT p.27).

## Decision

**호출 구조**
- 요청당 **입력 판정 1회 + 출력 판정 1회를 병렬**로 부른다. `candidate_answer`가 없으면 출력 판정은 부르지 않는다(ADR-0009).
- 개발·평가 실행은 실행 전에 예상 호출 수를 출력하고 상한을 넘으면 멈춘다(v4 `--estimate`·`--max-calls` 유지). 과한 호출은 경고 뒤 키 회수 사유다(OT p.27).
- 모든 LLM 호출은 기존 `llm.py`의 동시성 게이트 하나(세마포어 + AIMD, 단일 재시도 계층)를 지난다. 서버는 프로세스 1개라 게이트가 전체에 걸린다.

**모델 파라미터**
- `reasoning_effort="low"`, `verbosity="low"`, `response_format` json_schema strict, `max_completion_tokens` ≥800. `temperature`·`max_tokens`·`logprobs`는 보내지 않는다.
- 기동할 때 대회 엔드포인트에 한 번 시험 호출해 지원하지 않는 파라미터를 끈다.
- 호출당 타임아웃 약 12초, 호출마다 재시도 1회까지(요청당 최대 4호출), 요청 마감 25초 안에서만.

**입력 판정**
- 문맥: **첫 학습자 발화 + 최근 6턴**(토큰 상한 안). 첫 발화는 버튼 문구·역할극 설정이 있는 자리라 남긴다. 판정 대상인 마지막 학습자 발화는 따로 표시한다. 학습자 발화 전체는 구분자로 감싼 데이터로 다룬다.
- 결정적 신호(ADR-0002·0003)는 힌트로 넣는다.
- 스키마 순서: `evidence`(≤2문장) → `category` → `decision`. 범주가 decision을 결정적으로 정한다는 것이 공개 세트로 확인되면 decision은 코드로 계산한다.
- **투표는 k=1.** 3표 다수결은 호출 수를 늘리면서 효과가 측정되지 않았다(Skeptic·Pragmatist·Critic 모두 반대). 실험 후보는 "2표 병렬, 엇갈리면 1표 추가"(평균 약 2.2호출, Critic)이고 설정 플래그 뒤에 둔다.

**출력 판정**
- 입력: 마지막 학습자 발화 + 후보 답변 + 검색한 KB 섹션(ADR-0010). "근거는 KB만, 일반 상식은 근거 아님"을 명시하되, 자료와 모순되지 않는 일반 설명의 라벨은 공개 세트로 확인한다.
- 출력 필드는 **근거 없는 부분 + `safe`** 두 가지. `grounded`는 코드에서 `근거 없는 부분이 없음`으로 계산해 두 필드가 모순되지 않게 한다.
- 근거 없는 부분을 고르는 방식은 ADR-0005(v5 수정)를 따른다: 코드가 답변을 문장으로 나눠 번호를 붙이고, 판정기는 문장 번호를 고르고, 응답에는 그 문장의 원문을 넣는다.

## Alternatives Considered

### 입력·출력을 한 번의 호출로 통합
- **Pros**: 호출 수 절반, 공통 문맥 한 번
- **Cons**: 의도 분류와 구간 추출은 형태가 다른 과제라 간섭이 크다. nano는 과제를 쌓을 때 가장 크게 떨어졌다
- **Why not**: 병렬이면 지연은 느린 쪽 하나이고, 추가 비용은 공통 문맥 재전송뿐이다

### 입력 판정 3표 다수결(초안)
- **Pros**: 표 비율로 임계값을 맞출 수 있다
- **Cons**: 요청당 4호출, 키 회수·429 위험. 공개 세트 크기는 모르지만 수백 문항 규모라면(약 300이면 block F1 신뢰구간 ±0.05–0.07) 몇 %p 이득은 구별되지도 않는다
- **Why not**: council 세 목소리 모두 반대해 초안을 바꿨다

### 오래된 턴부터 잘라 최근 N턴만
- **Why not**: 앞에서 설정한 역할극·버튼 문구가 사라진다(Critic). 첫 발화를 고정으로 남긴다

### `reasoning_effort="minimal"`
- **Why not**: 판단 과제에서 정확도가 무너진다는 근거가 있다(R12 §4). 지연 ablation으로만 잰다

## Consequences

### Positive
- 요청당 nano 호출 최대 2회(재시도 제외), 지연은 약 4초 p95 수준(측정 필요)
- 두 필드(grounded·unsupported)가 구조상 모순되지 않는다

### Negative
- k=1이면 block 임계값을 확률로 맞출 수 없다. 정책 문구와 경계 사례로만 조정한다
- 첫 발화 + 6턴을 넘는 긴 대화의 중간 턴은 보지 못한다

### Risks
- 프록시의 실제 처리량·지연이 미지수(키 한도는 따로 없음) → 11/10 이후 채점과 같은 부하(동시 64, 가드 약 300요청)까지만 시험한다. 그보다 센 시험은 과한 호출로 볼 수 있어 운영진에게 먼저 묻는다(OT p.26–27). 채점기가 실제로 더 적게 동시 호출하면 전체 시간이 길어진다(Critic) → 요청 하나의 p95를 가능한 한 낮춘다
- `safe`의 정의가 "정답을 그대로 준 답변"까지 포함할 수 있다(Critic) → 공개 세트 라벨로 확인한 뒤 프롬프트를 쓴다
