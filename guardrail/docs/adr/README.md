# 가드레일 ADR

가드레일 설계 결정 기록. 요구사항은 [PRD](../PRD.md), 근거 자료는 [research/](../research/README.md).
새 결정은 [template.md](template.md)를 복사해 다음 번호로 쓴다. 바뀐 결정은 지우지 않고 `superseded by ADR-NNNN`로 표시한다.

**우선순위**: v6(2026-11-10, [R13 council](../research/R13-design-research.md))에서 고친 ADR은 맨 위의 "v6 수정" 블록이 그 아래 "v5 수정" 블록과 본문보다 우선한다. v6 블록이 없는 부분은 v5 블록, 그다음 본문 순이다. v6 기준 결정은 0012–0014와 각 v6 블록이다(결정 D1–D10은 [R13 §3](../research/R13-design-research.md#3-결정-d1d10)). 각 블록의 GR 번호는 그 판의 PRD 기준이다(v5 블록은 PRD v5, v6 블록은 PRD v6).

표기: `SPEC §N`은 [competition/SPEC.md](../../../competition/SPEC.md)의 절 번호(v6 기준). `OT §N`은 [competition/ot-facts.md](../competition/ot-facts.md)의 절 번호, `OT p.N`은 OT PDF의 쪽 번호다(v5 이전 블록). `R13-A`는 [공개 세트 분석](../research/R13-public-set-analysis.md), `R13-B`는 [외부 조사](../research/R13-B-external-research.md), `R13-B2`는 [라이브러리](../research/R13-B2-libraries.md)다.

| ADR | 제목 | 상태 | 날짜 |
|-----|------|------|------|
| [0001](0001-korean-normalization-skeleton-view.md) | 한국어 정규화와 골격 뷰 (v6: 정규화본 + 붙인 뷰, 골격 뷰 미사용, Kiwi 미사용) | proposed, v6 수정 | 2026-10-30 |
| [0002](0002-input-cascade-rules-then-llm.md) | 입력 판정: 규칙 → LLM (v6: 학습 분류기 허용하되 보조, 직접 결정은 저신호만) | proposed, v6: 일부 superseded by 0012·0013 | 2026-10-30 |
| [0003](0003-deterministic-crisis-first.md) | 위기 탐지를 결정적으로 먼저 (v6: `emotional_distress`는 allow인 범주 하나, 어휘 목록 삭제) | proposed, v6 수정 | 2026-10-30 |
| [0004](0004-judge-model-family.md) | 판정 모델은 생성과 다른 계열 | superseded by 0011 | 2026-10-30 |
| [0005](0005-sentence-level-grounding-with-quote-check.md) | 문장 단위 근거 판정 (v6: 구간은 규칙 구간, nano 구간은 ablation) | superseded by 0014 | 2026-10-30 |
| [0006](0006-evaluation-scope.md) | 평가 범위 (v6: 400행 짝 묶음 교차 검증, 채점 코드 import, 빌드당 3회) | proposed, v6 수정 | 2026-10-30 |
| [0007](0007-parallel-two-call-structure.md) | 입력 판정과 생성 병렬, 요청당 판정 2회, 차단 권한 4곳 | superseded by 0011 (0009도 대체) | 2026-10-30 |
| [0008](0008-input-role-split-jev.md) | 입력 단계 역할 분담: Jev는 탈옥·인젝션 고신뢰 차단, LLM은 정책 판단 | superseded by 0011 | 2026-10-30 |
| [0009](0009-stateless-http-judge.md) | `/guard`는 상태 없는 HTTP 판정기 (v6: `TEAM_TOKEN`, 튜터와 같은 서버(선택), D8 기본값, 422·예외 본문 제거) | proposed, v6 수정 | 2026-11-09 |
| [0010](0010-guard-side-kb-retrieval.md) | 가드 쪽 KB 검색 (v6: `##` 조각, 발화 + 답변 한 덩어리 질의, 1·2위 점수 차 게이트) | proposed, v6 수정 | 2026-11-09 |
| [0011](0011-llm-call-structure.md) | LLM 호출: 입력·출력 2회 병렬, gpt-5-nano low (v6: 조건부 3번째 호출 ablation, SDK 재시도 0, 전역 게이트) | proposed, v6 수정 | 2026-11-09 |
| [0012](0012-category-derived-decision.md) | 입력 판정: 범주만 출력하고 decision은 코드가 도출 | proposed | 2026-11-10 |
| [0013](0013-public-set-knn-and-leakage.md) | 공개 세트 kNN few-shot과 합성 데이터, 누수 방지 | proposed | 2026-11-10 |
| [0014](0014-output-three-tier-and-safe-rule.md) | 출력 판정: 3단 출력과 규칙 구간, safe 규칙 | proposed | 2026-11-10 |
