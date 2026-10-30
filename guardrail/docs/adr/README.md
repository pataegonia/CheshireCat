# 가드레일 ADR

가드레일 설계 결정 기록. 요구사항은 [PRD](../PRD.md), 근거 자료는 [research/](../research/README.md).
새 결정은 [template.md](template.md)를 복사해 다음 번호로 쓴다. 바뀐 결정은 지우지 않고 `superseded by ADR-NNNN`로 표시한다.

| ADR | 제목 | 상태 | 날짜 |
|-----|------|------|------|
| [0001](0001-korean-normalization-skeleton-view.md) | 한국어 정규화와 골격 뷰 | proposed | 2026-10-30 |
| [0002](0002-input-cascade-rules-then-llm.md) | 입력 판정: 규칙 → LLM, 학습 분류기는 P2 | proposed | 2026-10-30 |
| [0003](0003-deterministic-crisis-first.md) | 위기 탐지를 결정적으로 먼저 | proposed | 2026-10-30 |
| [0004](0004-judge-model-family.md) | 판정 모델은 생성과 다른 계열 | proposed | 2026-10-30 |
| [0005](0005-sentence-level-grounding-with-quote-check.md) | 문장 단위 근거 판정 + 인용 실재 확인 | proposed | 2026-10-30 |
| [0006](0006-evaluation-scope.md) | 평가 범위: 정적 세트 + 하네스, 확장 평가는 P2 | proposed | 2026-10-30 |
| [0007](0007-parallel-two-call-structure.md) | 입력 판정과 생성 병렬, 요청당 판정 2회, 차단 권한 4곳 | proposed | 2026-10-30 |
| [0008](0008-jev-judge-backend-option.md) | 판정 백엔드 선택지로 TypeSafe Jev | proposed | 2026-10-30 |
