# 가드레일 ADR

가드레일 설계 결정 기록. 요구사항은 [PRD](../PRD.md), 근거 자료는 [research/](../research/README.md).
새 결정은 [template.md](template.md)를 복사해 다음 번호로 쓴다. 바뀐 결정은 지우지 않고 `superseded by ADR-NNNN`로 표시한다. v5(대회 기준)에서 고친 ADR은 맨 위에 날짜를 단 "v5 수정" 블록이 본문보다 우선한다.

표기: `OT §N`은 [competition/ot-facts.md](../competition/ot-facts.md)의 절 번호, `OT p.N`은 OT PDF의 쪽 번호다.

| ADR | 제목 | 상태 | 날짜 |
|-----|------|------|------|
| [0001](0001-korean-normalization-skeleton-view.md) | 한국어 정규화와 골격 뷰 (v5: 두 뷰 유지, 신호는 힌트, 혼동 사전은 팀 작성) | proposed, v5 수정 | 2026-10-30 |
| [0002](0002-input-cascade-rules-then-llm.md) | 입력 판정: 규칙 → LLM (v5: 결정적 신호는 힌트, 정의상 확실한 것만 직접 결정) | proposed, v5 수정 | 2026-10-30 |
| [0003](0003-deterministic-crisis-first.md) | 위기 탐지를 결정적으로 먼저 (v5: `emotional_distress` 힌트로 축소) | proposed, v5 수정 | 2026-10-30 |
| [0004](0004-judge-model-family.md) | 판정 모델은 생성과 다른 계열 | superseded by 0011 | 2026-10-30 |
| [0005](0005-sentence-level-grounding-with-quote-check.md) | 문장 단위 근거 판정 (v5: 문장 번호 선택 / 구절 정렬) | proposed, v5 수정 | 2026-10-30 |
| [0006](0006-evaluation-scope.md) | 평가 범위 (v5: 공개 세트 + 채점식 재현) | proposed, v5 수정 | 2026-10-30 |
| [0007](0007-parallel-two-call-structure.md) | 입력 판정과 생성 병렬, 요청당 판정 2회, 차단 권한 4곳 | superseded by 0011 (0009도 대체) | 2026-10-30 |
| [0008](0008-input-role-split-jev.md) | 입력 단계 역할 분담: Jev는 탈옥·인젝션 고신뢰 차단, LLM은 정책 판단 | superseded by 0009 | 2026-10-30 |
| [0009](0009-stateless-http-judge.md) | `/guard`는 상태 없는 HTTP 판정기 | proposed | 2026-11-09 |
| [0010](0010-guard-side-kb-retrieval.md) | 가드 쪽 KB 검색: 섹션 단위, 문자 2-gram BM25, 문장별 질의 | proposed | 2026-11-09 |
| [0011](0011-llm-call-structure.md) | LLM 호출: 입력·출력 2회 병렬, gpt-5-nano low, 투표 k=1 | proposed | 2026-11-09 |
