# guardrail

대회 `POST /guard` 판정기(상태 없는 HTTP 엔드포인트, PRD v5). 담당 danielcho02.

- 요구사항: `docs/PRD.md` (GR-xx, v5) · 대회 사실: `docs/competition/ot-facts.md`(기준은 런박스 `SPEC.md`) · 계약: `docs/contract/guard.schema.json`
- 설계 결정: `docs/adr/`(v5 기준은 0009–0011과 0001·0002·0003·0005·0006의 "v5 수정" 블록) · 조사: `docs/research/`(v5 기준은 R12)
- 구현 계획: `docs/master-plan.md` v22(§1–§3·§8·§9가 v5, 나머지는 v4 기준이라 코드 재계획 대기) · 쓸 스킬: `docs/skills.md`
- 명령 (이 폴더에서): `uv sync` · `uv run pytest` · `uv run ruff check .` · `uv run ruff format .`
- 대회 규칙: 채팅 `gpt-5-nano`·임베딩 `text-embedding-3-small`만. 모델 파일, 외부 API, 인터넷 사전·데이터셋, 다른 LLM이 만든 데이터 묶음은 서버에 넣지 않는다. 출처는 `docs/competition/provenance.md`.
- **서버 로그는 시간·길이·상태 코드·오류만.** 요청·답변 본문을 저장하면 실격이다. API 키·토큰은 환경변수로만.
- `/tutor` 코드는 수정하지 않는다. `/tutor`와 맞닿는 결정은 PRD §9에 제안으로만 쓴다.
