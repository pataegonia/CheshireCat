# ADR-0009: `/guard`는 상태 없는 HTTP 판정기

**Date**: 2026-11-09
**Status**: proposed
**Deciders**: danielcho02 (council 2026-11-09: Architect·Skeptic·Pragmatist·Critic)
**PRD**: GR-01, GR-09, GR-12 (v5 번호: GR-01, GR-05, GR-08, GR-11)

## v6 수정 (2026-11-10, R13 council)

이 블록이 본문보다 우선한다(이 ADR은 v5에서 새로 썼으므로 본문이 v5다). PRD v6 GR-01·GR-09·GR-12 기준.
- **인증**: 토큰은 환경변수 `TEAM_TOKEN`에서 읽는다. 코드·커밋에 쓰지 않고 LLM API 키와 다른 값을 쓴다(SPEC §9). `Authorization: Bearer` 값을 `hmac.compare_digest`로 비교하고, 틀리면 401과 짧은 JSON 오류로 거절한다. SPEC은 상태 코드를 정하지 않는다. 401은 시작용 `competition/server.py`가 `HTTPException(401)`을 내는 것을 근거로 한다. 이것만 200이 아니다.
- **`/tutor`와 같은 서버(선택)**: SPEC은 `/tutor`와 `/guard`를 한 서버에 두라고 요구하지 않는다. 시작용 server.py는 둘을 한 앱에 둔다. 같은 서버(프로세스)로 둘지는 팀 선택(PRD §9 D4, 리더 결정). 가드는 APIRouter로 만들어 어느 쪽이든 붙는다. `redirect_slashes=False`. API 키는 어느 쪽이든 팀 키 하나라(SPEC §7.1), 한 프로세스를 함께 쓰면 전역 LLM 게이트가 중요해진다(ADR-0011 v6). 그때 KB 색인·kNN 색인·LR·게이트를 함께 쓴다.
- **항상 200**: 인증 실패를 뺀 모든 경우 200 + 규격 JSON 하나(v5와 같음, SPEC §3).
- **422·예외 본문 제거**: FastAPI 기본 422 응답은 요청 입력값을 되돌려 주고, SDK 예외 메시지에는 요청 본문이 실릴 수 있다([R13 §1 S6](../research/R13-design-research.md#1-근거-정리-research-ops-형식), [R13-B2 §3-5](../research/R13-B2-libraries.md#3-5-서버-fastapi--uvicorn)). 검증 오류 핸들러를 덮어써 읽을 수 있는 만큼 판정하고 200으로 답한다. 오류 로그는 예외 종류·오류 코드만 남긴다(SPEC §8, GR-12). uvicorn 접근 로그도 허용 항목만.
- **`output.category`**: 문자열이면 무엇이든 된다(SPEC §5, 채점 안 함). `input.category`를 복사한다. `input.category`는 10범주 밖이면 형식 오류이므로 nano 출력이 enum 밖이면 실패로 본다(ADR-0012).
- **요청 예산 약 20초**(v5 25초): 채점 제한 30초(SPEC §3) 안에서 터널·대기열 여유를 둔다([R13 D10](../research/R13-design-research.md#3-결정-d1d10)). 재시도 규칙은 ADR-0011 v6.
- **기본값(D8)**: 아래 본문의 기본값을 바꾼다([R13 D8](../research/R13-design-research.md#3-결정-d1d10), PRD GR-09).
  - 입력 nano 실패(재시도 후): kNN 다수 범주와 LR 범주가 같으면 그 범주, 다르면 LR 범주. LR이나 kNN이 꺼져 있으면 있는 쪽을 쓰고, 둘 다 없으면 `normal`. decision은 범주에서 도출(ADR-0012). v5의 결정적 신호 우선순위 목록은 쓰지 않는다.
  - 출력 nano 실패: 3단 출력에서 nano만 빼고 계산한다(①·②, 그 밖은 설정 `output.failure_default`, 기본 `grounded` = `true/[]` | `ungrounded` = `false/[]`). `output.failure_default`는 출력 nano가 실패했을 때와 `nano_tier3`가 꺼져 nano 판정을 쓰지 않을 때의 ④에만 적용한다. `nano_tier3`가 켜져 있고 nano가 근거 있음이라고 한 ④는 늘 `true/[]`다. ①·②에 안 걸리고 ③·④까지 오는 행의 공개 세트 grounded 비율이 약 32/(32+17) ≈ 0.65라 `true/[]` 기대값(약 1.3)이 `false/[]`(1)보다 높다. 규칙 ②가 꺼지거나 게이트로 빠지면 block이 아닌 행의 근거 없음 비율이 35/67 ≈ 0.52라 `false/[]`가 조금 낫다(가드 88.31 대 87.79, [R13-A Q4](../research/R13-public-set-analysis.md#q4-가드-점수-상하한과-민감도) 계산값이며 nano로 잰 값이 아님). 최종 설정에서 S3.1이 재고 정한다(ADR-0014).
  - `safe`: 늘 규칙(ADR-0014). v5의 "숨긴 문자·금지 표현·PII 신호면 false"는 "이모지 밖 태그 문자면 false"만 남는다.
  - 요청 파싱 실패: 못 읽으면 `allow`·`normal`, 본문에 `candidate_answer` 키가 있으면 `output`은 `output.failure_default`(기본 `true/[]`)·`safe=true`. 출력 nano 실패와 같은 규칙 하나다(PRD GR-09).
- 본문 대안의 "실패 시 기본값 `block`"은 다시 열지 않는다. 입력 nano가 실패해도 보조 판정기가 범주를 낸다.

## Context

v4는 튜터를 감싸는 Python 패키지였다. 입력 판정과 생성을 합류시키고, 차단·수리·응답 문구까지 가드가 정했다(ADR-0007). 대회에서는 채점기가 `/guard`를 `/tutor`와 따로 부르고, 가드는 판정 JSON만 돌려준다. 요청 사이에 상태를 이어 쓸 수 없고, 형식 오류·시간 초과는 그 문항을 틀린 것으로 센다. 로그에는 시간·길이·상태 코드·오류만 남길 수 있다(OT §3, §10).

## Decision

- `/guard`는 **요청 하나 → 판정 JSON 하나**인 순수 함수다. 세션 객체, 세션 위험 R 누적, 응답 문구, 수리는 없다. 멀티턴 문맥은 매 요청의 `messages`에서 다시 만든다(ADR-0011).
- **인증 실패를 뺀 모든 경우 HTTP 200 + 규격 JSON.** 예외·마감 초과는 기본값 응답으로 바꾼다. 요청 검증 오류는 `messages`(와 `candidate_answer`)를 읽을 수 있으면 판정하고, 못 읽으면 기본값 응답을 낸다. 원본 본문에 `candidate_answer` 키가 있으면 `output` 기본값도 넣는다(PRD GR-08). 인증 실패 응답은 `SPEC.md`를 따른다(OT: 맞지 않는 토큰은 거절). 확인 전 기본값은 401과 짧은 JSON 오류 본문. `/guard/` 리다이렉트를 만들지 않는다.
- **요청 마감 25초.** 그 안에서 호출마다 재시도 1회까지. 마감이 되면 그때까지의 결과와 기본값으로 응답한다.
- **기본값**(설정으로 바꿀 수 있음):
  - 입력: 마지막 학습자 발화의 결정적 신호가 있으면 우선순위(jailbreak_injection > emotional_distress > pii_disclosure > low_signal_input)로 하나를 골라 그 범주와 PRD §3의 decision(임시값 포함)을 쓰고, 없으면 `allow`·`normal`(PRD GR-08).
  - 출력: `grounded=true`, `unsupported=[]`, `safe`는 출력 쪽 결정적 신호(숨긴 문자·금지 표현·PII)가 있으면 `false`, 없으면 `true`.
- `candidate_answer`가 없으면 출력 판정 호출을 하지 않고 `output`을 생략한다(OT: 후보 답변이 있을 때만 필수). 빈 문자열일 때 처리는 `SPEC.md`를 따른다. 확인 전 기본값은 키가 있으면 `output`을 넣는다(빈 답변은 `grounded=true`·`unsupported=[]`·`safe=true`).
- `output.category`는 채점하지 않으므로 `input.category`를 복사한다. 응답은 매번 스키마로 검사한다.
- 서버 로그는 시간·길이·상태 코드·오류만. 판정 사유와 본문은 메모리에서만 쓰고 버린다.

## Alternatives Considered

### v4 래퍼 구조 유지(가드가 튜터를 감쌈)
- **Pros**: 기존 코드(`pipeline.py`)와 문서 재사용
- **Cons**: 채점기는 `/guard`를 따로 부른다. 래퍼의 차단·수리는 가드 점수에 쓰이지 않는다
- **Why not**: 채점 구조와 맞지 않는다

### 실패 시 기본값 `block`
- **Pros**(Skeptic): 모두 허용하면 block F1이 0이므로, block이 많은 세트에서는 실패를 block으로 두는 편이 F1에 유리할 수 있다
- **Cons**: block이 소수면 정밀도를 깎는다. 공개 세트 분포를 아직 모른다
- **Why not**: 지금은 `allow`로 두되 설정 값으로 남기고, 11/10 공개 세트의 block 비율을 보고 다시 정한다

### 세션 상태를 서버 메모리에 저장
- **Why not**: 규격이 상태 이어 쓰기를 금지하고, 채점 요청을 모아 쓰는 것은 실격 사유다(OT §10)

## Consequences

### Positive
- 테스트가 쉽다. 입력 JSON → 출력 JSON 표로 회귀 테스트한다
- 장애가 형식 오류로 번지지 않는다

### Negative
- v4의 `Session`, 세션 위험 R, 수리, 거절·위기 응답, `trace.py` 원문 기록이 모두 폐기된다
- 멀티턴 문맥을 매번 다시 만들어 입력 토큰이 늘어난다

### Risks
- 기본값이 실제 분포와 반대일 수 있다 → 공개 세트의 필드별 다수 클래스로 다시 정한다
- 로그 규칙 위반은 실격이다 → 로그 코드 리뷰와 실행 후 로그 grep을 수용 기준에 넣는다(PRD GR-11)
