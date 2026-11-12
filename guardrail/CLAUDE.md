# guardrail

대회 `POST /guard` 판정기(상태 없는 HTTP 엔드포인트, PRD v6). 담당 danielcho02.

- 요구사항: `docs/PRD.md` (GR-01–GR-14, v6) · 대회 규격: 저장소 `competition/SPEC.md`(기준), 채점 코드 `competition/grader/scoring.py` · 계약: `docs/contract/guard.schema.json`
- 설계 결정: `docs/adr/`(v6 기준은 0012–0014와 각 ADR의 "v6 수정" 블록, v6 블록이 v5 블록·본문보다 우선) · 조사: `docs/research/`(v6 기준은 R13, 결정 D1–D10은 `R13-design-research.md`)
- 구현 계획: `docs/master-plan.md` v23, Phase 문서 `docs/plan/C1–C4`(GP0–GP4는 v4 기록) · 쓸 스킬: `docs/skills.md`
- 명령 (이 폴더에서): `uv sync` · `uv run pytest` · `uv run ruff check .` · `uv run ruff format .`
- lock은 저장소 루트 `uv.lock` 하나다(uv workspace, 가드·튜터 공통). 의존성을 바꾸면 `uv lock`이 루트 lock을 갱신한다. `guardrail/uv.lock`은 만들지 않는다.
- 대회 규칙: 채팅 `gpt-5-nano`·임베딩 `text-embedding-3-small`만. 모델 파일, 외부 API, 인터넷 사전·데이터셋, 다른 LLM이 만든 데이터 묶음은 서버에 넣지 않는다. 출처는 `docs/competition/provenance.md`.
- **서버 로그는 시간·길이·상태 코드·오류만.** 요청·답변 본문을 저장하면 실격이다. API 키·토큰은 환경변수로만.
- 커밋 메시지에 `Co-Authored-By:` 줄, PR 제목·본문에 `🤖 Generated with Claude Code` 줄을 넣지 않는다(`pr-policy / attribution` 검사가 PR 본문을 막는다. 설정은 저장소 `.claude/settings.json`의 `attribution`).
- `/tutor` 코드는 수정하지 않는다. `/tutor`와 맞닿는 결정은 PRD §9에 제안으로만 쓴다.
