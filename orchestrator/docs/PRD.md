# 오케스트레이터 · 통합 평가 PRD

| 항목 | 내용 |
|---|---|
| 문서 상태 | v4 초안 (2026-11-08) — 튜터 PRD(#39) 리뷰 반영 |
| 담당 | 플랫폼 담당 (오케스트레이터 · 통합 평가 · 튜터 기능 일부, PM 겸임) |
| 상위 문서 | `docs/PRD.md` (기획서 · 팀 PRD) |
| 참고 | 가드 인터페이스 제안서(`guardrail/docs/interface.md`) |
| 범위 | 오케스트레이터 서버(OR) · 통합 평가(EV) · 실행 환경. 튜터와의 분담은 §5에 확정. 가드 내부와 튜터 내부(인덱싱 · 검색 · 근거 판정 · 생성)는 다루지 않는다 |
| 설계 원칙 | **빠르게, 싸게, 틀리지 않게.** 요청 바깥(API · 세션 · 어댑터 · 검증 · 렌더링 · 기록)만 맡고 요청 안의 흐름(동시 실행 · 합류 · 취소 · 재생성)은 가드에 둔다 |

---

## 1. 목표와 비목표

**목표**
- G1 `POST /v1/chat` 한 번으로 세션 복원 → `guard.respond()`(어댑터 안에서 교수 전략 · 인용구 검증) → 렌더링 → 저장이 끝난다.
- G2 튜터는 가드 타입에 의존하지 않고 HTTP 계약만 구현하면 연결된다.
- G3 요청 전체 p95 ≤ (TBD)초, 요청당 LLM 호출 평균 ≤ 5회. 지연 · 비용은 설계로 줄이고 측정으로 증명한다.
- G4 "교재 안에서만 답했다"를 인용구 검증 통과율이라는 수치로 보인다.
- G5 통합 평가가 실제 가드 + 실제 튜터 상태의 발표 수치를 낸다.

**비목표**
- 가드 판정 로직 · 임계값 · 프롬프트 · 응답 문구
- 튜터 인덱싱 · 청킹 · 검색 · 근거 판정(기권) · 생성 프롬프트 (튜터 PRD)
- 실제 교사 연락 채널, 사용자 가입, 다중 테넌트, 운영 배포 자동화

## 2. 사용자와 시나리오

| 사용자 | 시나리오 | 보장할 것 |
|---|---|---|
| 학생 | 질문 → 출처 달린 답. 후속 · 재질문 | 세션 유지, 출처 표시, 응답 시간 상한 |
| 학생 (위기) | 위기 발화 | 알림 이벤트, 원문 미저장 |
| 평가자 | clone → 실행 → 데모 · 전후 비교 | `docker compose up`, README, 가드 토글 |
| 팀원 | 자기 모듈을 서비스에 붙여 확인 | 튜터 스텁, 계약 테스트, 통합 테스트 |
| 통합 평가 실행자 | 평가 세트 배치 → 리포트 | 평가 모드 인스턴스, CLI 러너 |

## 3. 구성과 요청 흐름

가드 단계 번호(①~⑨)는 가드 PRD, 튜터 단계 번호(T1~T4)는 튜터 설계의 것이다. 그림은 Claude 공유 페이지 「튜터 경로 요청 흐름」.

```
POST /v1/chat {session_id?, user_id, grade_band, message}
  ▼  [오케스트레이터 · 직렬 · 규칙만 · LLM 0회]
  API 키 검증 → 세션 ID HMAC 검증 → Redis에서 세션 복원
    (이력 ≤ 8턴 · R · level · flow_step · covered_sections · fail_count · last_chunks)
  → Helpy 흐름 상태 전이 (level_check → plan → explain → quiz → summary)
  → 검색 힌트 준비: flow_step · last_chunks · 직전 intent (교수 전략은 아직 고르지 않음 — 어댑터가 T1 결과를 본 뒤 고름)
  → 요청 시간 예산 시작 (기본 30s, 초과 시 진행 중 호출 취소 → 안전 응답)
  ▼
  guard.respond(message, history, adapter, session)
  │  [가드 소유 구간. 오케스트레이터는 어댑터로만 참여]
  │  ①~④ 입력 가드 (직렬) ─(③ 확실한 규칙)─▶ 차단 → Verdict 반환
  │  ├──────────────┬──────────────────────────────────────────────┐  [동시 실행]
  │  ▼              ▼                                               ▼
  │  Jev(선택)       ⑤ 입력 판정 (LLM 1회)        [어댑터] retrieve (light 슬롯) ──HTTP──▶ [튜터] T1 질의 이해 (LLM 1회)
  │                                                                  → T2 유형별 검색 (BM25+벡터 → 리랭커 → 부모 확장)
  │                                                                  → T3 기권 게이트 (애매 구간만 자기 평가 1회)
  │                                               [어댑터] 청크 + retrieval_status · retrieval_context(intent · required_aspects) · usage 수신
  │                                                  → S# 번호 부여 · 출처 매핑 보관 · Chunk(sid, source, text)
  │                                                  → 교수 전략 선택(규칙): T1 intent × level × fail_count → 템플릿 ID · 스캐폴딩 단계
  │                                               ⑥ 검색 가드 (청크 인젝션 검사)
  │                                               기권이면 → 생성 생략, 기권 응답 + 가장 가까운 단원
  │                                               [어댑터] generate (generator 슬롯) ──HTTP──▶ [튜터] T4 생성 (LLM 1회)
  │                                                  instruction = 가드 지시 + 템플릿 ID · 스캐폴딩 단계 · retrieval_context 그대로 전달 · 비스트리밍 · system_extra
  │                                               [어댑터] 인용구 검증 (LLM 0회)
  │                                                  문장마다 quote ⊂ 인용 청크? 아니면 문장 제거
  │                                                  제거율 > 임계 → instruction 붙여 재생성 1회 (가드 ⑨ 켜져 있으면 가드에 위임)
  │  └──────────────── 합류 (가드 규칙: ADR-0007/0008) ──────────────┘
  │     차단 → 생성 버림 / 전환·부분 → instruction 붙여 재생성 / 허용 → 계속 / 위기 → 위기 템플릿
  │  ▼  [직렬]
  │  ⑦ 코드 검사 → ⑧ 교환 판정 (LLM 1회) → ⑨ 수리 (재생성 시 어댑터 generate(instruction=) 재호출) → _finalize (위기 우선)
  ▼
  Verdict {response, action, crisis_flag, stages, fallback_infra, …}
  ▼  [오케스트레이터 · 직렬 · LLM 0회]
  출처 렌더링: [S#] → 교재 › 단원 › 섹션 (어댑터가 보관한 매핑, 없는 번호는 ⑦이 이미 제거)
  → 세션 갱신 (규칙): 이력 추가 · R 저장 · flow_step · covered_sections · fail_count · last_chunks
  → crisis_flag면 알림 이벤트 발행 (P2)
  → 트레이스 JSONL: 요청 ID 아래 단계별 지연 · 호출 수 · 토큰 · 검증 결과 (서비스 모드는 원문 제외)
  → 응답 {session_id, request_id, response, action, citations[], crisis_flag, latency_ms}
```

LLM 호출: 기본 4회(⑤ · T1 · T4 · ⑧), 최대 6회(T3 자기 평가 · ⑨ 재생성 또는 형식 재요청), 기권 시 3회, ③ 규칙 차단 시 0회. 임베딩 호출은 이 상한에 세지 않고 비용에만 넣는다. 가드 판정은 가드 게이트를 직접 지나고, 튜터 호출은 어댑터가 `/retrieve`를 `light` 슬롯, `/generate`를 `generator` 슬롯으로 감싸 동시성을 맞춘다(OR-06).

장애 시: 튜터 타임아웃 · 서킷 브레이커 열림 → 어댑터가 예외 반환 → 가드 생성 실패 행으로 안전 응답(fail-closed). 엘리스 API 429 · 장애 → 게이트 재시도 · AIMD, 예산 초과 시 `fallback_infra`.

### 3.1 디렉토리 구성 (플랫폼 소유 부분)

```
cheshirecat/
├─ .github/
│  ├─ CODEOWNERS                  # /guardrail/ 줄은 그대로, 플랫폼 디렉토리 줄 추가
│  └─ workflows/orchestrator-ci.yml   # orchestrator · e2e_eval · contracts 경로 필터
├─ docker-compose.yml             # orchestrator · tutor(또는 stub) · redis · (ui)
├─ .env.example
├─ docs/
│  ├─ PRD.md                      # 기획서 · 팀 PRD
│  └─ adr/                        # 팀 공통 결정
│
├─ contracts/                     # HTTP 스키마 (pydantic). 변경은 담당자 합의 후 버전업
│  └─ src/contracts/
│     ├─ chat_api.py              # /v1/chat 요청 · 응답 · 오류
│     └─ tutor_api.py             # /retrieve · /generate · /info · /health · (/ingest)
│
├─ orchestrator/
│  ├─ Dockerfile                  # 빌드 컨텍스트 = 저장소 루트, guardrail/ 복사 후 편집 모드 설치
│  ├─ pyproject.toml · uv.lock
│  ├─ docs/PRD.md                 # 이 문서
│  ├─ src/orchestrator/
│  │  ├─ main.py                  # FastAPI 앱, Guard.from_config() 1회, 워밍업
│  │  ├─ settings.py              # 환경 변수 (pydantic-settings)
│  │  ├─ api/                     # chat · health · (sessions · traces)
│  │  ├─ chat.py                  # 세션 복원 → 검색 힌트 → guard.respond() → 검증 · 렌더링 → 저장
│  │  ├─ pedagogy/                # Helpy 흐름 상태기계 · 교수 전략 선택(어댑터가 generate 직전에 호출) · 템플릿 (OR-07)
│  │  ├─ adapters/
│  │  │  ├─ tutor_http.py         # TutorAdapter 구현 · keep-alive · 타임아웃 · llm.slot
│  │  │  └─ chunk_mapper.py       # 튜터 청크 → Chunk(sid, source, text), 출처 · 타입 · 점수 매핑
│  │  ├─ verify/quotes.py         # 인용구 검증 (OR-05)
│  │  ├─ render/citations.py      # [S#] → 교재 › 단원 › 섹션
│  │  ├─ session/                 # Redis 저장 · Turn 이력 · 학습 상태
│  │  ├─ tracing/                 # 요청 ID별 단계 지연 · 호출 · 토큰 · 검증 결과
│  │  └─ llm_gate.py              # 가드 게이트 설정 주입 · 슬롯 · 사용량 집계 (OR-06)
│  └─ tests/
│     ├─ unit/
│     ├─ adapter/                 # 가드 TutorAdapter 계약 스위트
│     └─ integration/             # 튜터 스텁과 끝-끝
│
├─ e2e_eval/                      # 통합 평가 (EV)
│  ├─ src/e2e_eval/               # runner · metrics(가드 지표는 guardrail.eval 재사용) · report · load
│  ├─ configs/                    # 가드 켬/끔 · 모델 · top-k 변형
│  ├─ datasets/                   # 세트 목록 · 분할 · 홀드아웃 봉인
│  └─ results/                    # <date>-<config-hash>-<sha>.json, FROZEN.txt
│
├─ devtools/tutor_stub/           # 가드 mock_tutor를 HTTP 계약으로 감싼 스텁
├─ ui/                            # 데모 UI (P1)
│
├─ guardrail/                     # 가드 담당 · 수정하지 않음
└─ tutor/                         # 튜터 담당 · 내부 구조 자유, api.py가 계약 엔드포인트
```

튜터와의 분담은 §5에 확정돼 있다. 파서 · 구조 트리 · 근거 판정은 `tutor/`, 교수 전략 · 인용구 검증 · 세션은 `orchestrator/`.

## 4. 기능 요구사항 — 오케스트레이터 (OR)

우선순위: P0 제출 필수 · P1 수치 · 완성도 · P2 여유 시.

### OR-01 외부 API (P0)
- `POST /v1/chat` `{session_id?, user_id, grade_band, message}` → `{session_id, request_id, response, action, citations[], crisis_flag, latency_ms}`
- `GET /health`, `GET /ready` (가드 설정 · Redis · 튜터 `/health`)
- 스키마 `contracts/chat_api.py` (pydantic, 버전 필드). 오류 `{error:{code,message,request_id}}`
- 인증 API 키 1종. 세션 ID HMAC 서명
- (P1) `GET /v1/sessions/{id}`, `DELETE /v1/sessions/{id}`, `GET /v1/traces/{request_id}`

### OR-02 세션 (P0)
- 가드 `Session(user_id, grade_band, risk, last_block_norm)` + `Turn` 이력 + **학습 상태**(`level`, `flow_step`, `covered_sections[]`, `fail_count{concept}`, `abstained[]`, `last_chunks[]`)를 Redis에 직렬화
- 이력 상한 8턴, TTL 24h (설정값). 서비스 모드는 원문 대신 `user_hmac`만
- 학습 상태 갱신은 규칙만 (TR-12)

### OR-03 가드 호출 (P0)
- 시작 시 `Guard.from_config(path, overrides)` 1회. overrides = 모델 · 키 · 동시성 · 예산 · 모드 (환경 변수)
- 요청마다 `guard.respond(message, history, adapter, session, instruction=)` 1회. 방식 B 금지
- `Verdict`의 `response · action · crisis_flag · stages · fallback_infra`를 그대로 사용. 문구 변경 금지

### OR-04 튜터 어댑터 (P0)
- `TutorAdapter` 구현: `retrieve(question, history) -> list[Chunk]`, `generate(question, history, chunks, *, system_extra, instruction) -> str`
- HTTP keep-alive 연결 풀, 타임아웃(retrieve 8s · generate 20s, 설정값 — 11/13 측정 뒤 조정), 재시도 1회, 서킷 브레이커
- S# 번호 부여(튜터가 돌려준 근거 묶음 순서 그대로), `Chunk(sid, source, text)` 변환(`text` = 튜터 `raw_text`), 출처 매핑 · 청크 타입 · `retrieval_status` · `retrieval_context` · `usage` 보관
- `/retrieve` 요청에 검색 힌트(`flow_step`, `last_chunks`, 직전 `intent`)를 넣고, `/generate` 요청에는 `/retrieve`가 돌려준 `retrieval_context`를 그대로 넘긴다
- 튜터 HTTP 오류(503 `INDEX_NOT_READY`, 502/504 `UPSTREAM_*`, 502 `GENERATION_FORMAT_ERROR`)는 기권과 구분해 예외로 올려 가드 생성 실패 행(fail-closed)을 타게 한다
- 청크 래핑 · 비스트리밍 · `regenerate = generate(instruction=)` 담당
- `/retrieve`는 `guard.llm.slot("light")`, `/generate`는 `guard.llm.slot("generator")` 안에서 호출(동시성 제어). 교수 전략은 `/retrieve` 결과를 본 뒤 `generate` 호출 직전에 고른다(OR-07)
- 튜터 HTTP 계약: `POST /retrieve` · `POST /generate` · `GET /info` · `GET /health` · (선택) `POST /ingest`. 스키마 `contracts/tutor_api.py`
- 가드 하네스가 `TUTOR_MODULE`로 이 어댑터를 import할 수 있게 패키지 경로 고정 (어댑터 한 벌)

### OR-05 인용구 검증과 출처 렌더링 (P0)
- `/generate` 응답의 `sentences[]`(문장 · `kind` · `evidence[{sid, quote, quote_found}]`)를 입력으로, `claim` 문장의 quote가 인용 청크 `raw_text`에 **정규화 후 부분 문자열**로 존재하는지 대조(튜터의 `quote_found`는 참고만 하고 어댑터가 다시 확인). `non_claim` 문장은 근거 없이 허용. 실패 문장은 제거하고 `stages`에 기록. 제거율이 설정 임계(기본 30%)를 넘으면 `instruction`을 붙여 재생성 1회 요청(가드 ⑨와 중복 호출 방지: 가드 ⑨가 켜져 있으면 제거만 하고 재생성은 가드에 위임)
- `[S#]` → "교재 › 단원 › 섹션" 치환. 매핑에 없는 번호는 가드 ⑦이 이미 제거
- 정규화 규칙: 공백 · 문장부호 · 전각/반각 통일, 코드 블록은 원문 그대로

### OR-06 LLM 호출 관리 (P0)
- 가드 판정 호출은 가드 `llm.py` 게이트를 직접 지난다. 소유(설정 · 집계) = 오케스트레이터, 구현 = 가드
- 튜터는 별도 프로세스라 게이트를 import하지 않고 자체 `LLMClient`로 엘리스 API를 부른다. 대신 어댑터가 `/retrieve`를 `light` 슬롯, `/generate`를 `generator` 슬롯으로 감싸 **동시성만** 제어한다. 슬롯: `judge` · `generator` · `light`
- 토큰 · 비용 집계: 가드 몫은 `Verdict.stages`, 튜터 몫은 `/retrieve` · `/generate` 응답의 `usage`(LLM 호출 수 · 임베딩 호출 수 · 토큰 · 단계별 지연). 요청 ID 단위로 합산해 트레이스에 저장
- 요청당 LLM 호출 상한 6은 가드 판정 2 + 튜터 몫 최대 4(T1 · 자기 평가 · T4 · 재생성 또는 형식 재요청). 임베딩 호출은 상한에 세지 않는다
- 서버 수준: 요청 동시성 상한, 세션별 속도 제한, 요청 전체 시간 예산(기본 30s), 예산 초과 시 진행 중 호출 취소 → 안전 응답

### OR-07 Helpy 흐름 상태기계와 교수 전략 (P1)
- 상태: `level_check → plan → explain → quiz → summary`, 세션 `flow_step`에 저장. 전이는 규칙(첫 턴 · 직전 턴 `intent` · 다룬 섹션 수 · 학생 응답)
- **선택 시점은 어댑터의 `/generate` 호출 직전**이다. `guard.respond()` 전에는 질문 유형을 알 수 없으므로(유형은 튜터 T1이 `/retrieve` 안에서 분류), 오케스트레이터는 세션에서 검색 힌트(`flow_step` · `last_chunks` · 직전 `intent`)만 준비해 `/retrieve` 요청에 넣는다
- 어댑터가 `/retrieve` 응답의 `retrieval_context`(`intent` · `required_aspects` · `context_link`)를 받은 뒤, 규칙(호출 없음)으로 `intent` × `level` × `fail_count` → 템플릿 ID · 스캐폴딩 단계(힌트 → 유도 질문 → 근거 제시)를 정하고, 가드가 넘긴 `instruction`(전환 · 부분 · 재생성 지시)에 합쳐 `/generate`에 보낸다
- 템플릿 ID 목록(`concept` · `example` · `compare` · `followup` · `quiz` · 기본)은 튜터 담당과 함께 정하고 `contracts/`에 둔다. 튜터는 ID별 생성 지시만 가진다
- 응답 뒤 T1 `intent`와 `retrieval_status`를 세션에 저장해 다음 턴 힌트로 쓴다

### OR-08 트레이스 · 로그 (P0)
- 요청 ID 아래 단계별 지연 · 호출 · 토큰 · 인용구 검증 결과 · 어댑터 지연을 JSONL로
- 서비스 / 평가 모드 로그 경로 분리, 서비스 모드 원문 금지

### OR-09 서비스 / 평가 모드 (P0)
- 서비스: PII 마스킹 필수, 원문 미저장, `faults` 비어 있음
- 평가: 별도 인스턴스 · 설정 · 로그 경로, 원문 역추적 허용

### OR-10 알림 이벤트 (P2)
- `crisis_flag` 또는 `action == crisis` → Redis 큐 이벤트(`request_id`, `user_hmac`, `crisis_tier`, 시각). 소비자는 범위 밖

### OR-11 배치 실행 (P1)
- 통합 평가 · 교재 인덱싱은 CLI 러너로 실행. 큐 · 워커는 시간이 남으면(P2)

### OR-12 튜터 스텁 (P0)
- 가드 `mock_tutor`를 HTTP 계약으로 감싼 `devtools/tutor_stub/`. 통합 테스트 · 데모 · CI용

### OR-13 실행 환경 · 저장소 (P0)
- `docker-compose.yml`: orchestrator · tutor(또는 stub) · redis · (ui). `.env.example`
- 오케스트레이터 이미지는 `guardrail/` 폴더째 복사 후 편집 모드 설치
- CI `orchestrator-ci.yml` 경로 필터, `uv sync --locked → ruff → mypy → pytest`. CODEOWNERS 추가. `guardrail/` 수정 금지

### OR-14 데모 UI (P1)
- 채팅 1화면: 질문 · 답변 · 출처 · 판정(허용 / 전환 / 부분 / 거절 / 위기) · 지연. 가드 켬/끔 토글로 전후 비교

### OR-15 제출물 (P0)
- README(실행 · 환경 변수 · 평가 실행 · 모듈 경계 · 영상 링크), 발표 자료 PPT/PDF(수치 각주), 시연 영상 3~5분 공개 링크

### OR-16 지연 단축 (P0) — 설계 항목
| 수단 | 내용 | 기대 효과 |
|---|---|---|
| 병렬 | ⑤ 판정 ∥ 튜터 경로는 가드가 이미 동시 실행. 어댑터 retrieve 안에서 BM25와 벡터 검색은 튜터가 병렬(튜터 PRD에 요청) | 직렬 대비 판정 1회분 절감 |
| 조기 종료 | ③ 규칙 차단, 기권 판정이면 ④ · ⑧ 생략 | 기권 요청 3회 → 2회 |
| 경량 모델 분리 | 튜터 ① 질의 이해와 ③ 자기 평가는 `light` 슬롯(작고 빠른 모델) | 호출 1~2회의 지연 ↓ |
| 자기 평가 조건부 | ③ 자기 평가는 점수가 애매 구간일 때만 | 평균 호출 수 ↓ |
| 컨텍스트 상한 | top-k 3~5, 부모 섹션 확장 상한(토큰), 요약 노드로 긴 섹션 대체 | 생성 입력 토큰 · 지연 ↓ |
| 연결 재사용 | 튜터 · API HTTP keep-alive, Redis 연결 풀 | 왕복 지연 ↓ |
| 캐시 | 임베딩 캐시(질문 정규화 키), 재작성 결과 캐시(세션 + 정규화 질문 키), 인덱스 메모리 상주 | 반복 · 후속 질문 ↓ |
| 시간 예산 | 단계별 타임아웃과 전체 예산, 초과 시 취소 | p99 꼬리 제거 |
| 워밍업 | 시작 시 가드 설정 · 인덱스 · 모델 연결 미리 | 첫 요청 지연 ↓ |

### OR-17 정확도 개선 (P0) — 설계 항목
| 수단 | 내용 |
|---|---|
| 인용구 검증 | OR-05. 지어낸 문장을 기계적으로 제거 |
| 기권 게이트 보정 | θ_low · θ_high를 통합 평가의 "교재에 없는 질문" 세트로 보정, ablation 기록 |
| 유형별 검색 | 예제 우선 · 대조 검색 · 직전 청크 유지를 `instruction` 검색 힌트로 전달 |
| 재작성 제한 | 최근 N턴 + 요약만 재작성 입력으로 (장기 표류 방지) |
| 교수 프롬프트 준수 모델 선택 | 11/9 모델 목록에서 생성 모델을 교수 지시 준수율로 비교해 선택 (C14) |
| ablation | top-k · 리랭커 유무 · 컨텍스트 청킹 유무 · 인용구 검증 유무를 통합 평가에서 비교 |

### OR-18 비용 통제 (P1)
- 슬롯별 예산, 실행당 `--max-calls`, 요청당 LLM 호출 상한(6회, 임베딩 제외) 초과 시 안전 응답
- 모델별 토큰 단가표(`configs/pricing.yaml`)로 비용 집계

## 5. 튜터와의 분담 (확정, 튜터 PRD #39 §9 D-T1~D-T5)

| ID | 기능 | 소유 | 근거 |
|---|---|---|---|
| TU-P1 | 파서 · 구조 트리(교재 › 단원 › 섹션 › 블록, 원문 위치) | **튜터** (TU-01) | 파서 · 청킹과 한 몸. 따로 두면 두 번 파싱 |
| TU-P2 | 근거 판정 · 기권 게이트(네 상태: answerable · partial · needs_clarification · insufficient_evidence) | **튜터** `/retrieve` (TU-08) | 검색 신호를 가진 쪽이 판정 |
| TU-P3 | 세션 학습 상태 · Helpy 흐름 상태기계 | **오케스트레이터** (OR-02 · OR-07) | 튜터는 stateless |
| TU-P4 | 질문 유형 → 교수 전략(템플릿 ID · 스캐폴딩) 선택 | **어댑터** (OR-07) — T1 `intent`를 받은 뒤 | 유형 분류는 튜터 T1, 전략 선택은 플랫폼 |
| TU-P5 | 인용구 검증(문장 제거 · 재생성 요청) | **어댑터** (OR-05) | 튜터는 `quote_found` 표시까지 |

튜터 담당 몫: 파서(형식별) · 청크 타입 분류 · 컨텍스트 청킹 · 하이브리드 인덱스 · 리랭커 · 요약 노드 · ① 질의 이해 · ② 검색 · ④ 생성 프롬프트.

## 6. 기능 요구사항 — 통합 평가 (EV)

### EV-01 러너 (P0)
- `/v1/chat` 경유, 실제 가드 + 실제 튜터. 설정 파일로 가드 켬/끔 · 모델 · top-k 등 변형
- 실행당 `--max-calls`, 결과 `e2e_eval/results/<date>-<config-hash>-<sha>.json`

### EV-02 지표 (P0)
| 지표 | 정의 |
|---|---|
| 파이프라인 과잉 거절 | 정상 질문 중 거절 · 부분 응답 비율 (무방비 대비) |
| 이중 기권 | 튜터 기권 + 가드 차단이 겹친 비율 |
| 지연 | 요청 전체 p50 · p95 · p99, 단계별 분해 |
| 호출 · 비용 | 요청당 LLM 호출 수 · 토큰 · 비용 |
| 버린 생성 비율 | 합류 차단으로 취소된 생성 비율 |
| 인용구 검증 통과율 | 생성 문장 중 quote 대조 통과 비율, 제거 문장 수 |
| 가드 지표 재측정 | `guardrail.eval` 지표 코드 재사용(차단율 · 위기 재현율 · 근거 이탈) |

### EV-03 부하 시험 (P1)
- 동시성 20 · 35 · 50, 정상 질문 `fallback_infra` ≤ 1%, p95 변화 기록

### EV-04 ablation (P1)
- top-k(3/5/8) · 리랭커 · 컨텍스트 청킹 · 인용구 검증 · 경량 모델 분리를 켜고 끄며 EV-02 비교

### EV-05 데이터 · 재현 (P0)
- 가드 `assign_split()`으로 분할 · 홀드아웃 봉인, 홀드아웃은 동결 뒤 1회
- `results/FROZEN.txt`에 SHA · 설정 해시 · 시각. 발표 수치는 이 파일에서만

## 7. 비기능 요구사항

| 항목 | 기준 |
|---|---|
| 응답 시간 | 전체 예산 30s(설정), p95 목표 (TBD, 11/13 후) |
| 동시성 | 요청 동시성 상한 (TBD), 부하 시험 20~50 통과 |
| 비용 | 요청당 호출 평균 ≤ 5, 상한 6 |
| 가용성 | 튜터 · API 장애 시 fail-closed 안전 응답 |
| 보안 | API 키, HMAC 세션, 원문 미저장, 링크 · 이미지 허용 목록 |
| 로그 보존 | 평가 무제한, 서비스 30일 |
| 재현성 | SHA · 설정 해시 · 분할 ID |
| 실행 | `docker compose up`, Python 3.12+, uv |

## 8. 합격선

| 지표 | 합격선 | 측정 |
|---|---|---|
| 끝-끝 통과 | 스모크 세트 전부 응답, 예외 0 | 통합 테스트 |
| 과잉 거절 | 가드 하네스 값 + (TBD)%p 이내 | EV-02 |
| 지연 p95 | (TBD)초 | EV-02 |
| `fallback_infra` | ≤ 1% @ 동시성 20~50 | EV-03 |
| 인용구 검증 통과율 | ≥ (TBD)% (보고 필수) | EV-02 |
| 어댑터 계약 | 가드 `test_tutor_contract` 통과 | CI |

## 9. 마일스톤 (플랫폼)

| 날짜 | 작업 |
|---|---|
| 11/7~8 | O0.1 골격 · CI · compose, O0.2 세션, O0.3 `/v1/chat`, O0.4 튜터 스텁 |
| 11/9 | OT · 데이터 확인, 모델 목록 · 한도 확인, 공통 결정 |
| 11/10~13 | O0.5 어댑터 · `guard.respond()` 연결, O0.6 인용구 검증 · 렌더링, O0.7 트레이스, O0.8 EV 러너 골격. **11/13 통합 1차** |
| 11/14~17 | O1.1 Helpy 흐름 · 교수 전략, O1.2 데모 UI, O1.3 지연 단축(OR-16), 부하 시험(16). **11/17 튜터 판정** |
| 11/18~20 | EV 전체 실행 · ablation, 정확도 보완(OR-17), 발표 자료 골격(18). **11/20 구현 종료 · 수치 동결 18:00** |
| 11/21 | 에러 수정, 시연 영상 녹화 · 편집 · 링크 |
| 11/22 | 최종 검토, 18:00 제출 |

## 10. 리스크

| 리스크 | 대응 |
|---|---|
| 실제 튜터 11/17 미연결 | 스텁으로 OR · EV 수치 확보, 보고서 표기 |
| 가드 ⑨ 재생성과 OR-05 재생성 중복 호출 | 가드 ⑨ 켜짐 여부로 분기(OR-05) |
| 경량 모델이 ML API에 없음 | 생성 모델 하나로 `light` 슬롯 대체, 동시성만 분리 |
| `/retrieve`가 8s를 넘김 (T1 + 임베딩 + 검색) | 자기 평가 끄기 · 독립 질문 T1 생략 · 타임아웃 조정 (튜터 PRD §6.2) |
| 인용구 검증이 코드 · 수식에서 오탐 | 코드 블록은 정규화 없이 원문 대조, 수식은 검증 제외 플래그 |
| API 한도 미공개 | 게이트 4로 시작, 답 오면 설정만 변경 |

## 11. 미결 사항

| 미결 | 기본값 | 늦어도 |
|---|---|---|
| 튜터 HTTP 스키마 (`contracts/tutor_api.py`) | 기본: `chunks` · `abstain` · `nearest_section` · `answer` · `sentences`. 확장: `retrieval_status` · `message` · `retrieval_context` · `source_locator` · `signals` · `usage` · `coverage` · `final_status`. `/retrieve` 요청 힌트 필드 이름은 플랫폼이 초안 | 11/10 |
| 출처 메타데이터 항목 | `source` 단일 문자열 | 11/10 |
| 경량 모델 · 생성 모델 선택 | 11/9 모델 목록 | 11/10 |
| p95 · 과잉 거절 · 통과율 합격선 | 통합 1차 후 | 11/13 |
