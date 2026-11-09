# 서버 자산 출처 (provenance)

작성 2026-11-09 · 담당 danielcho02 · 근거: [OT 사실 §10](ot-facts.md#10-규칙실격-25-26) · [PRD GR-13](../PRD.md#gr-13-규칙-준수-p0-신설) · [R12 §8](../research/R12-competition-baseline.md#8-v4-자산-출처-점검-gr-13)

서버(채점받는 `/guard`)에 들어가는 데이터·사전·예시·프롬프트의 출처를 적는다. 시연에서 코드와 함께 보여 준다. **새 자산을 서버에 넣는 PR은 이 표에 행을 더해야 한다.**

허용: 배포 자료와 그것을 가공한 것 · 팀이 직접 쓴 것(프롬프트·규칙 안의 예시 포함) · 외부 호출 없는 오픈소스 라이브러리.
불가: 미리 학습된 모델 파일 · 인터넷에서 받은 사전·목록·데이터셋 · 다른 LLM으로 만든 별도 예시·데이터 묶음 · 다른 팀 코드·프롬프트.

## 1. 서버에 들어가는 것

| 자산 | 출처 | 작성 | 상태 |
|---|---|---|---|
| `src/guardrail/preprocess.py` 자모 변환표·정규화 규칙 | 유니코드 한글 조합 규칙(공개 사양) | 팀(R5 조사 단계 참고 코드를 옮겨 고침, ADR-0001) | 있음 |
| `kb/<과목>/` 색인 | 대회 배포 자료 | 주최측 | 기동 때 색인(C1) |
| 결정적 신호 어휘·패턴(위기·PII·인젝션·저신호) | — | 팀이 직접 씀 | 작성 예정(GR-06). 다른 LLM이 만든 목록을 붙여 넣지 않는다(팀 방침. OT p.25는 규칙 안의 예시를 팀 작성으로 보지만 보수적으로 간다) |
| 판정 프롬프트 `input_judge.md`·`output_judge.md`와 그 안의 경계 사례 | 공개 세트 개발 분할을 보고 팀이 씀 | 팀 | 작성 예정(GR-04·GR-07). 공개 세트 원문을 그대로 붙여 넣지 않고 팀이 바꿔 쓴다 |
| 라이브러리 | PyPI: `openai`, `pydantic`, `pyyaml`, (예정) `fastapi`, `uvicorn`, `rapidfuzz` | 오픈소스, 모델·사전 없음, 외부 호출은 주최측 엔드포인트만 | R12 §6 |

## 2. 서버에 넣지 않는 것 (개발·평가 전용)

| 자산 | 출처 | 이유 |
|---|---|---|
| `data/seed/smoke_benign.jsonl`(50), 로컬 `smoke_attack.jsonl`(50)·`smoke_crisis.jsonl`(30) | Claude Code 서브에이전트(Sonnet) 초안 + 본인 검토. `data/README.md`에 명시, 레코드 값은 `source: "self"` | 다른 LLM이 만든 데이터 묶음 |
| `data/seed/textbook_placeholder.jsonl`(38절) | 같음 | 같음. v5에서는 `kb/`로 대체 |
| `data/seed/grounding/inject_prompt.md` | 팀 작성 프롬프트(산출물은 LLM 생성) | 산출물이 LLM 생성 데이터 |
| `prompts/attack_judge.md` | 팀 작성(AI 코딩 도구 보조) | v4 평가 전용, S0–S14 기준 |
| `resources/crisis_resources.yaml` | 공공 상담 번호 + 팀 문구, 기사 인용 | 가드 응답에 문구가 없어 불필요 |
| 외부 공개 데이터셋(wildguardmix, MultiJail 등) | 인터넷 | 저장소에 없음. 서버 밖 평가 사용도 운영진 확인 전에는 하지 않음 |
| `configs/*.yaml`의 `typesafe-ai/jev-1.14.0` | 외부 모델 | 코드 재계획에서 삭제 |

## 3. 시연 전 확인

- [ ] 위 1절에 없는 데이터·목록 파일이 서버 경로에 없다
- [ ] 서버 로그에 요청·답변 본문이 없다(PRD GR-11)
- [ ] `.env`·키가 git에 없다
- [ ] 제출한 커밋 해시가 `submission.json`의 `commit`과 같다
