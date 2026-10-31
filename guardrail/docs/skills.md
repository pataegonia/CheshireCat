# 가드레일 작업 스킬 명세

작성 2026-10-30 · 갱신 2026-10-31 · 담당 danielcho02 · `/ecc:skill-scout` 결과
대상: [master-plan.md](master-plan.md)의 슬라이스. 슬라이스마다 쓸 스킬은 각 Phase 문서([plan/](plan/))에도 적혀 있다.

## 찾은 방법과 결론

- 로컬 ECC 2.2.1에 설치된 스킬·에이전트를 이름으로 확인했다(2026-10-30). 아래 표의 이름은 모두 설치되어 있다. `dataviz`, `humanize-korean`, `code-review`는 ECC 밖 플러그인이다.
- 원격 검색은 하지 않았다. 필요한 작업이 모두 로컬 스킬로 덮인다: TDD, 평가 하네스, LLM 비용, 프롬프트, 보안 리뷰, 이슈 관리.
- **새 스킬은 만들지 않는다.** 가드레일 전용 절차(위기 경로 검증, 홀드아웃 1회 규칙)는 스킬이 아니라 슬라이스 수용 기준과 이 문서의 공통 루프로 강제한다.

## 모델 사용 규칙

| 작업 | 모델 |
|---|---|
| 설계 판단, 정책·판정 프롬프트 작성, 적대 검증(santa), 보안 리뷰, 평가 설계, 결과 해석 | Opus |
| 형식이 정해진 반복 작업: 데이터 변환·번역 초안, 로더, 문서 갱신, 이슈·라벨 작업, 단순 코드 리뷰 | Sonnet |
| 판단이 갈리는 결정 | 사용자에게 묻기 전에 `/ecc:council`(본 세션 + 독립 서브에이전트 3명, Opus)로 먼저 정리하고, 그래도 사용자 몫이면 묻는다 |

## 모든 슬라이스의 공통 루프

| 순서 | 할 일 | 스킬·에이전트 |
|---|---|---|
| 1 | 슬라이스 이슈와 Phase 문서를 읽고 테스트부터 쓴다 | `ecc:tdd-workflow`, `ecc:python-testing` |
| 2 | 구현하고 `ruff check` · `ruff format --check` · `pytest`를 돌린다 | `ecc:verification-loop` |
| 3 | 리뷰한다. 기본 리뷰어에 슬라이스별 리뷰어를 더한다 | 에이전트 `ecc:python-reviewer`(Sonnet) |
| 4 | 설계를 바꿨으면 ADR을 고치거나 새로 쓴다 | `ecc:architecture-decision-records` |
| 5 | PR(`Closes #n`) → CI green → 이슈 체크리스트 갱신 | `ecc:github-ops`, `ecc:git-workflow` |

## Phase별 표

### GP0 기반

| 슬라이스 | 스킬 | 에이전트(모델) | 쓰는 이유 |
|---|---|---|---|
| S0.1 CI·추적 | `ecc:github-ops`, `ecc:git-workflow` | — | 워크플로·라벨·마일스톤·이슈 |
| S0.2 계약·설정 | `ecc:api-design`, `ecc:python-patterns` | `ecc:type-design-analyzer`(Sonnet) | 판정 타입이 잘못된 상태를 못 만들게 |
| S0.3 LLM 게이트웨이 | `ecc:cost-aware-llm-pipeline`, `ecc:error-handling`, `ecc:documentation-lookup` | `ecc:silent-failure-hunter`(Opus), `ecc:docs-lookup`(Sonnet) | 재시도·예산·토큰 기록, 삼킨 예외 찾기, SDK 문서 확인 |
| S0.4 trace | `ecc:security-review` | `ecc:security-reviewer`(Opus) | 로그에 키·원문 PII가 남지 않게 |
| S0.5 데이터 로더 | `ecc:eval-harness` | `ecc:pr-test-analyzer`(Sonnet) | 분할·중복 제거 테스트가 실제 버그를 막는지 |
| S0.6 모의 튜터 | `ecc:python-patterns` | `ecc:rag-pipeline-reviewer`(Sonnet) | 최소 RAG가 베이스라인으로 쓸 만한지 |
| S0.7 하네스 v0 | `ecc:eval-harness`, `ecc:benchmark-methodology`, `ecc:santa-method`(`attack_judge.md`) | `ecc:mle-reviewer`(Opus), 독립 리뷰어 2명(Opus) | 지표 정의·통계·예산 상한, 채점 프롬프트 적대 검증 |
| S0.8 인터페이스 제안서 | `ecc:api-design` | `ecc:architect`(Opus) | 튜터 쪽 부담을 최소로 |

### GP1 출력 가드

| 슬라이스 | 스킬 | 에이전트(모델) | 쓰는 이유 |
|---|---|---|---|
| S1.1 근거 평가 세트 | `ecc:eval-harness`, `ecc:ai-regression-testing` | 생성·변환은 general-purpose(Sonnet), 유형 설계 검토는 Opus | 주입 유형 7종, 2개 계열 주입 |
| S1.2 매처·카나리아 | `ecc:regex-vs-llm-structured-text`, `ecc:tdd-workflow` | `ecc:python-reviewer`(Sonnet) | 결정적 매칭과 경계 규칙 |
| S1.3 출력 코드 검사 | `ecc:tdd-workflow` | `ecc:python-reviewer`(Sonnet) | |
| S1.4 문장 분리·사전 필터 | `ecc:benchmark`, `ecc:latency-critical-systems` | `ecc:performance-optimizer`(Sonnet) | 코드 단계 지연 예산 |
| S1.5 교환 판정 | `ecc:prompt-optimizer`, `ecc:cost-aware-llm-pipeline`, `ecc:santa-method` | `ecc:mle-reviewer`(Opus) | 실패 문장만 출력하는 프롬프트, 적대 검증 |
| S1.6 수리 | `ecc:error-handling` | `ecc:silent-failure-hunter`(Opus) | ⑧ 실패 시 닫히는지 |
| S1.7 출력 가드 평가 | `ecc:eval-harness` | `ecc:mle-reviewer`(Opus) | 임계값 스윕, 유형별 보고 |

### GP2 입력·위기·멀티턴

| 슬라이스 | 스킬 | 에이전트(모델) | 쓰는 이유 |
|---|---|---|---|
| S2.0 입력 평가 데이터 | `ecc:eval-harness` | 번역·형식 변환은 general-purpose(Sonnet) | 홀드아웃 봉인 |
| S2.1 위기 탐지·템플릿 | `ecc:santa-method`, `humanize-korean` | 독립 리뷰어 2명(Opus) | 위기 경로는 두 리뷰어가 모두 통과해야 한다. 템플릿 문구는 의미를 바꾸지 않고 다듬는다 |
| S2.2 규칙·GR-01 수용 | `ecc:regex-vs-llm-structured-text` | `ecc:python-reviewer`(Sonnet) | 확실한 패턴만 규칙으로 |
| S2.3 서비스 로그·PII | `ecc:security-review` | `ecc:security-reviewer`(Opus) | 체크섬, 허용 목록, HMAC |
| S2.4 입력 판정 | `ecc:prompt-optimizer`, `ecc:santa-method` | `ecc:mle-reviewer`(Opus) | 정책 프롬프트 400–600토큰 |
| S2.5 병렬 합류·장애 등급 | `ecc:error-handling`, `ecc:latency-critical-systems`, `ecc:santa-method`(위기 행) | `ecc:silent-failure-hunter`(Opus), `ecc:code-reviewer`(Sonnet), 독립 리뷰어 2명(Opus) | 취소·폴백 경로, 위기 행은 두 리뷰어 모두 통과 |
| S2.6 거절·전환 템플릿 | `humanize-korean` | — | 훈계하지 않는 짧은 문구 |
| S2.7 입력 쪽 평가·폭주 시험 | `ecc:eval-harness`, `ecc:benchmark` | `ecc:mle-reviewer`(Opus) | attack_judge–사람 κ 포함 |
| S2.8 세션 위험 | `ecc:python-testing`(hypothesis 속성 테스트) | `ecc:python-reviewer`(Sonnet) | |
| S2.9 검색 가드 | `ecc:security-review` | `ecc:rag-pipeline-reviewer`(Sonnet) | 간접 인젝션 |
| S2.10 Jev | `ecc:search-first`, `ecc:security-review` | `ecc:security-reviewer`(Opus) | 외부 전송 전제 조건 |

### GP3 통합·평가

| 슬라이스 | 스킬 | 에이전트(모델) | 쓰는 이유 |
|---|---|---|---|
| S3.1 튜터 어댑터 | `ecc:api-design` | `ecc:code-reviewer`(Sonnet) | |
| S3.2 주최측 평가셋 어댑터 | `ecc:eval-harness` | — | |
| S3.3 임계값 확정·곡선 | `ecc:eval-harness`, `dataviz` | `ecc:mle-reviewer`(Opus) | 개발 세트에서만 정한다 |
| S3.4 ablation | `ecc:benchmark-methodology` | `ecc:mle-reviewer`(Opus) | McNemar + Holm |
| S3.5 홀드아웃 1회 | `ecc:santa-method` | 독립 리뷰어 2명(Opus) | 보고 수치를 두 리뷰어가 검증 |
| S3.6 비용·지연·폭주 | `ecc:benchmark` | — | |
| S3.7 기능 동결 점검 | `ecc:quality-gate`, `ecc:security-review`, `code-review` | `ecc:security-reviewer`(Opus), `ecc:silent-failure-hunter`(Opus, `pipeline.py`), `ecc:doc-updater`(Sonnet) | 11/20 동결 전 마지막 점검 |

### GP4 발표

| 슬라이스 | 스킬 | 에이전트(모델) | 쓰는 이유 |
|---|---|---|---|
| S4.1 전후 비교 데모 | `ecc:e2e-testing`(시나리오 고정) | — | 데모가 매번 같은 결과를 내게 |
| S4.2 차트·표 | `dataviz` | — | 곡선·ablation·지표표 |
| S4.3 운영 사양 | `humanize-korean` | — | 1쪽 문서 |
| S4.4 보고서 가드 절 | `ecc:santa-method`, `humanize-korean` | 독립 리뷰어 2명(Opus) | 수치·출처 검증 |
| S4.5 확장 평가(P2) | `ecc:search-first`(garak·promptfoo·NeMo 버전 확인), `ecc:benchmark-methodology` | `ecc:docs-lookup`(Sonnet), `ecc:mle-reviewer`(Opus) | 같은 하네스 지표로 비교 |

## 쓰지 않는 것

- 웹·모바일 프레임워크 스킬(React, Django 등): 가드는 UI가 없다.
- `ecc:gan-*`, `ecc:loop-*`: 자율 루프는 위기·홀드아웃 규칙을 사람이 확인해야 해서 맞지 않는다.
- 발표 슬라이드 형식은 팀이 정한다. 가드 쪽은 차트와 표를 넘긴다(`ecc:frontend-slides`·`anthropic-skills:pptx`는 팀 형식이 정해지면 고른다).
