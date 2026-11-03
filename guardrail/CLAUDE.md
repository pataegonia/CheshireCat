# guardrail

튜터 앞뒤에 붙는 독립 가드레일 패키지. 담당 danielcho02.

- 요구사항: `docs/PRD.md` (GR-xx) · 설계 결정: `docs/adr/` · 조사: `docs/research/`
- 구현 계획: `docs/master-plan.md`(계약 §4가 기준) · Phase별 슬라이스: `docs/plan/GP*.md` · 쓸 스킬: `docs/skills.md`
- 명령 (이 폴더에서): `uv sync` · `uv run pytest` · `uv run ruff check .` · `uv run ruff format .`
- 튜터 코드는 수정하지 않는다. 튜터와 맞닿는 결정은 PRD §9에 제안으로만 쓴다.
- API 키는 환경변수로만. 로그에 원문 PII를 남기지 않는다.
