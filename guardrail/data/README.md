# 가드레일 평가 데이터

형식은 [master-plan §4.6](../docs/master-plan.md#46-데이터-evaldatasetspy)이고, 로더는 `src/guardrail/eval/datasets.py`(`load`, `assign_split`, `dedupe`)다.

커밋 규칙(§4.6): 정상 스모크만 본문을 커밋한다. 공격·위기 스모크는 이 저장소가 공개라서 로컬에만 두고(`guardrail/.gitignore`), 여기에는 건수·해시·작성 규칙만 적는다. `data/raw/`, `data/dev/`·`data/holdout/` 본문, `runs/`도 커밋하지 않는다.

## 스모크 세트 (`data/seed/`, 하네스 확인용, 모두 `split: dev`)

| 파일 | 건수 | 커밋 | sha256 |
|---|---|---|---|
| `smoke_benign.jsonl` | 50 | 함 | `5a47bbe4f38a096660da1cd56dcfc88a574f9a37d7db0ee97623df04829c937a` |
| `smoke_attack.jsonl` | 50 | **로컬 전용** | `c9115bfab40b266798156aa862b1b2a2296da5c4e1af6d13ea3190ad56af3b8f` |
| `smoke_crisis.jsonl` | 30 | **로컬 전용** | `964d06c4b1d7ab4fa6442c45f5ebc5f488c7c75d86070c0bd33b84401c43ee18` |

- 출처 `source: "self"`, 라이선스 `CC-BY-4.0`, 작성 danielcho02. 2026-11-03에 Claude Code 서브에이전트(Sonnet)가 초안을 쓰고 본인이 전수 검토했다.
- 구성(GP0 S0.5 표):
  - 정상: 교재 개념 20(우회 표기 정상 4 포함: chosung 2, codeswitch 2), 수식 8, 코드 6, 위험해 보이는 정상 6(`benign_risky`), 위기 어휘가 섞인 정상 4, 인사·잡담 6(`greeting`). 모두 `allow`.
  - 공격: 시드 30 + 변형 20(jamo 5, chosung 3, yamin 3, codeswitch 3, roleplay 3, honorific 3). S14 20, S9 10, S7 5, S10 5, S12 5는 `refuse`, S2 5는 `redirect`.
  - 위기: T1(S5) 20(direct 8, euphemism 6, evasion 6), T2(S6) 5, T3(S8) 5. `expected={action: crisis, crisis_tier}`.
- 작성 규칙:
  - 유해 범주는 요청 문장만 일반 수준으로 쓴다(구체 절차·재료·수치 없음).
  - 위기 문장에는 방법·도구·장소를 넣지 않는다.
  - 실제 이름·전화·학교명이 없다(`tests/test_datasets.py`가 전화·주민번호 패턴 0건을 확인).
  - 자모 변형은 손으로 쓰지 않고 음절을 낱자로 푸는 스크래치 스크립트로 만들었다(커밋하지 않음). 같은 시드와 `normalize`가 같은지를 테스트가 확인한다.
- id 접두어 `smk-`(공격 `smk-atk-0001`, 변형 `smk-atk-0001-jamo`, 정상 `smk-ben-`, 인사 `smk-grt-`, 위기 `smk-cri-`). 시드는 `seed_id == id`이고 변형은 시드의 id다. S2.0이 스모크 id를 `dedupe(fixed_dev=...)`에 넘기므로 다른 자체 세트 씨앗과 겹치지 않게 접두어를 둔다.
- 잃으면: 같은 지시서(GP0 S0.5 구성표·작성 규칙)로 다시 만든다. 하네스 확인용이라 수치 비교 대상이 아니다. 해시가 다르면 다시 만든 것이다.

## 외부 세트 (PRD §8)

주최측 dev·test, wildguardmix-test-ko, MultiJail ko, OR-Bench-hard, K-OverRefusal, JBB·XSTest·StrongREJECT는 **미반입**이다(S2.0·S3.2에서 받는다). 라이선스와 받는 법은 [R2 데이터셋 메모](../docs/research/R2-datasets.md)를 따른다. 비상업(NC) 라이선스 원문은 `data/raw/`(gitignore)에만, 평가 전용으로 둔다.

## 대체 교재 (`data/seed/textbook_placeholder.jsonl`, 커밋함)

| 항목 | 내용 |
|---|---|
| 제목 | 중학교 과학 1(대체본), 자체 집필 |
| 라이선스 | CC-BY-4.0, 작성 danielcho02. 2026-11-03에 Claude Code 서브에이전트(Sonnet)가 초안을 쓰고 본인이 수치·화학식을 검산했다 |
| 규모 | 38절(sci-01–sci-38), 절당 217–304자, 본문 약 9,300자 |
| 단원 | 물질의 상태 변화 8 · 밀도 5 · 광합성 8 · 힘과 무게 6 · 과학 탐구와 데이터 6(평균 내는 파이썬 코드) · 실험실 안전 5 |
| sha256 | `5b41d72b7ddb98aeae5b918137ff9329bf980115c8fec9202b360bc54c34a70a` |

- 형식: 한 줄에 한 절 `{"source", "title", "text"}`. 기존 교과서·위키 문장을 옮기지 않았다.
- 계획의 "약 40절"보다 2절 적다(단원별 절 수 합 38). 모의 튜터 검색 기준(손으로 쓴 10문항 top-3 ≥8/10)은 이 교재로 통과한다.
- 주최측 교재가 11/6까지 오면 이 파일을 바꾸고 S1.1 근거 세트를 그 교재로 만든다. 뒤에 오면 GP1 수치는 "대체 교재 기준"으로 적는다(master-plan §8).
