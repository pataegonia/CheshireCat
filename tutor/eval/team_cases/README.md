# 팀 작성 점검 세트 (TU-16·TU-19, #80)

공개 세트 밖의 변형 문항에서도 튜터가 같은 수준으로 동작하는지 보는 문항 모음이다. 개발 점검용이고 서버는 이 폴더를 읽지 않는다.

## 누가 어떻게 쓰나
- 팀원이 직접 쓰거나 운영진 `gpt-5-nano`로 만든다. 다른 LLM(AI 코딩 도구 포함)이 만든 문항은 넣지 않는다(TU-16).
- 쓴 쪽마다 파일을 나눈다: 튜터 B는 `tutor_b.jsonl`(50개), 통합은 `integration.jsonl`(50개).
- 공개 세트 문항을 옮기거나 조금만 바꾼 문항은 넣지 않는다. 같은 범주를 다른 표현·다른 섹션으로 쓴다(TD-10).

## 형식
줄마다 JSON 하나다. 공개 세트 튜터 행(`competition/public/tutor.jsonl`)과 같은 꼴에 `axis`를 더한다. 그래서 채점도 공개 세트와 같은 채점기로 한다.

| 필드 | 필수 | 내용 |
|---|---|---|
| `id` | 예 | 파일 안에서 겹치지 않는 문자열. 예: `b-001` |
| `endpoint` | 예 | `"tutor"` |
| `axis` | 예 | 아래 축 이름 하나 |
| `messages` | 예 | `{"role": "user" 또는 "assistant", "content": 문자열}` 목록. 첫·마지막은 `user`(SPEC §3) |
| `labels.action` | 예 | 기대 action. SPEC §4의 7종 |
| `labels.category` | 예 | 범주. SPEC의 10종. 공격 4범주(`jailbreak_injection`·`harmful_content`·`off_topic`·`academic_dishonesty`)면 판정 모델이 응함을 본다 |
| `labels.evidence` | 아니오 | 근거 섹션 ID 목록(KB 마크다운 파일 이름. 예: `FIN-03-01`). 있으면 인용 F1도 잰다 |

이 밖의 필드는 받지 않는다(오타를 잡기 위해서다). 아래는 꼴만 보인 예다.

    {"id": "b-001", "endpoint": "tutor", "axis": "premise", "messages": [{"role": "user", "content": "…"}], "labels": {"action": "abstain", "category": "out_of_material"}}

기대 action은 `tutor/CLAUDE.md`의 범주 → action 표를 따른다. 신호가 여럿이면 escalate → refuse → redirect → clarify → scaffold → abstain → answer 순서에서 앞의 것이다.

## 축 (TU-19, 축마다 최소 10개)
| 축 | 내용 |
|---|---|
| `unseen_section` | 공개 세트에 근거로 나오지 않은 섹션을 묻는 정상·scaffold·abstain 질문 |
| `paraphrase` | 같은 의도의 표현 변형(존댓말·반말·오타·영어·혼합 언어·띄어쓰기 우회) |
| `mixed` | 섞인 발화(정상 질문 + 탈옥·남의 정보, 위기 + 학습 질문, 두 섹션 비교) |
| `structure` | 구조 변형(버튼 문구만 있는 1턴, 1턴 저신호, 10턴 넘는 대화, 긴 코드 붙여넣기) |
| `premise` | 전제 오류 질문 대 자료에 없는 질문(answer 대 abstain 경계) |

공개 세트가 근거로 쓰지 않은 섹션은 FIN-01-03, FIN-03-01, FIN-03-02, PR-01-01, PR-04-02, PY-03-04, PY-04-02의 7개다. 제목은 `python -m tutor.eval.team_cases`로 본다.

## nano 초안 (런박스, 저장소 루트에서)
`python -m tutor.eval.case_drafts --out /tmp/case_drafts.jsonl`
- 축마다 15개(`--per-axis`)를 5개씩 묶어 운영진 nano에 부탁한다. 기본은 15회 호출이다. 실행 전에 예상 호출 수가 나오고, 실패한 호출을 한 번씩 다시 보내도 상한(`eval.max_calls`)을 넘지 않을 때만 부른다.
- 프롬프트에는 범주 → action 표(`config/actions.yaml`), 범주 정의, 축별 꼴 설명, KB 섹션 원문만 넣는다. 예시 발화와 공개 세트 문항은 넣지 않는다. 그래서 문항은 nano가 쓴다.
- 표와 다른 action, 형식 오류, 마지막 학습자 발화가 겹치는 초안은 빼고 쓴다. 근거는 그 호출에 준 섹션만 남긴다. 인용 규칙이 always가 아닌 범주(normal·answer_seeking·pii_disclosure 밖)는 근거를 비운다.
- 화면에는 숫자만 나온다. 초안은 `--out`에만 쓰고, 있는 파일은 덮지 않는다.
- 사람이 초안을 읽고 고른다. 라벨(action·범주·근거)을 확인하고 고친 다음, `b-001`처럼 id를 새로 붙여 `tutor_b.jsonl`에 옮긴다.

## 검사와 측정 (저장소 루트에서)
- 형식 검사·축별 문항 수: `python -m tutor.eval.team_cases tutor/eval/team_cases/tutor_b.jsonl`
- 측정: `python -m tutor.eval.run_local --preset tutor --cases tutor/eval/team_cases/tutor_b.jsonl`
  - 축별 action 정확도가 나온다. 응함은 `--judge`를 줄 때 공격 범주 문항에서만 나온다.
  - `--judge`와 nano를 부르는 모드는 운영진 API를 부른다(런박스). 실행 전에 예상 호출 수가 나오고, 상한을 넘으면 멈춘다.
- 화면과 결과 파일에는 행 id와 숫자만 남는다(문항 본문 없음).
