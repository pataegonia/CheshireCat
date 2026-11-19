# 질문 색인 측정 (T4.1) 런박스 실행 안내

`tutor/eval/qindex_probe.py`는 서버가 읽지 않는 측정 도구다. 조각마다 "답할 수 있는 질문"과 "답할 수 없는
질문"을 nano로 만들어 임베딩하고, 개발 행에서 검색 적중과 자료 밖 신호를 잰다.

## 규칙

- **생성 입력은 교재 조각 본문뿐이다.** 프롬프트는 고정 지시문(`SYSTEM_PROMPT`, 버전 `PROMPT_VERSION`)과
  조각 본문 하나다. 공개 세트·채점 질문은 넣지 않는다(테스트가 단언한다).
- 생성물은 런박스에서 gpt-5-nano로 만든 것만 서버에 넣을 수 있다. 이 도구의 출력은 측정용이고
  `tutor/eval/runs/` 아래(git 제외)에 둔다. 커밋하지 않는다.
- 홀드아웃 묶음 행은 읽지 않는다(id만 보고 건너뛴다). 개발 160행만 쓴다.

## 명령 (저장소 루트, `OPENAI_*`·`EMBEDDING_*`가 있는 런박스 셸)

```bash
python -m tutor.eval.qindex_probe gen --out tutor/eval/runs/qindex --yes
python -m tutor.eval.qindex_probe embed --dir tutor/eval/runs/qindex
python -m tutor.eval.qindex_probe eval --dir tutor/eval/runs/qindex
```

`gen`은 `--yes` 없이 돌리면 예상 호출 수만 낸다. **전체를 돌리기 전에 먼저 3조각만 돌린다.**

```bash
python -m tutor.eval.qindex_probe gen --out tutor/eval/runs/qindex --limit 3 --yes
```

`questions.jsonl`에 54줄(3 × 18)이 생겼는지, `gen_errors.jsonl`이 없는지(특히 `finish_reason: length`) 본 뒤
전체를 돌린다. 같은 명령을 다시 돌리면 끝난 조각(18줄이 다 있는 조각)은 건너뛰고 실패했거나 쓰다 끊긴
조각만 다시 한다.

## 예상 호출 수와 시간 (어림)

| 단계 | 호출 | 시간 |
|---|---|---|
| gen | nano 244회(조각당 1회, 실패하면 1회 더 보내 최대 488회), 동시 8개 | 10~20분 |
| embed | 질문 약 4,400문장(244 × 18)을 64개씩 약 69회 | 1~3분 |
| eval | 개발 질의 320개(160행 × 질의 2종) 5회 + KB 조각 244개 4회. 캐시가 있으면 0회 | 1분 안쪽 |

모든 단계가 부르기 전에 예상 호출 수를 `config/tutor.yaml`의 `eval.max_calls`와 견준다. gen은 nano 429가
20회 이어지면 종료 코드 6으로(run_local과 같은 규칙), 인증 오류가 3회 이어지면 종료 코드 1로 끝나고 남은
조각은 보내지 않는다.

## 출력 파일 (`<폴더>/`)

| 파일 | 내용 |
|---|---|
| `questions.jsonl` | 줄마다 `chunk_id`(`<섹션 ID>#<섹션 안 순서>`)·`section_id`·`subject`·`kind`(answerable/unanswerable)·`lang`(ko/en)·`question`·`prompt_version` |
| `gen_errors.jsonl` | 실패한 조각: `chunk_id`·`error`(LLM 오류 코드 또는 `count:`·`empty:`·`lang:` + 칸 이름)·`finish_reason`(`length`면 출력 상한 2000토큰에 잘림) |
| `emb.npy`·`emb.json` | 질문 임베딩(float32, questions.jsonl 줄 순서)과 순서 메타·questions.jsonl의 sha256·임베딩 모델 이름. gen을 더 돌렸으면 embed를 다시 돌린다(eval이 맞지 않으면 멈춘다) |
| `dev_query_emb.npy`·`.json` | 개발 질의 임베딩 캐시(행 id·질의 종류·질의 해시·모델 이름만, 본문 없음) |
| `chunk_emb.npy`·`.json` | KB 조각 임베딩 캐시(현재 서버 방식 재현과 영어 질문의 왕복 필터에 쓴다) |
| `eval_report.json` | 검색 적중표와 자료 밖 신호표(`--report`로 경로를 바꾼다) |
| `eval_rows.jsonl` | 행 id별 신호값·문턱 판정·각 방식의 1위 섹션(질문 본문 없음) |

## 보고서 읽는 법

- `retrieval.<방식>.<그룹>` = `n`·`hit1`·`hit5`. 방식: `bm25`(지금 검색 질의), `rule_a`(규칙 A 질의의 BM25),
  `bm25_or_rule_a`(둘 중 하나라도 맞음), `server`(현재 서버 방식: `retrieve`를 `english_embedding` 켠 채로
  불러 영어 질의는 조각 임베딩 순위, 한국어 질의는 BM25. 영어 행 기준선은 이 줄의 `en`을 본다),
  `qindex`(질문 색인 단독), `rrf`(BM25 + 질문 색인),
  `qindex_roundtrip`·`rrf_roundtrip`(왕복 필터 적용). `_last`가 붙으면 질의가 마지막 발화만이다.
  그룹: `all`, `ko`/`en`(마지막 발화 언어), `PY`/`PR`/`FIN`(정답 섹션 과목).
- `oom.<질의>.<신호>` = `auc`(out_of_material 16행 대 normal 79행)·`ci95`(묶음 단위 부트스트랩)·
  `caught`/`of`(5겹 문턱으로 잡은 자료 밖 행)·`flagged_answer`(잘못 잡은 정답 answer 행)·`flagged`(잡은 모든 행).
  `direction`이 -1인 신호(`s_pos`)는 부호를 뒤집어 "클수록 자료 밖"으로 맞춘 뒤 잰 값이다.
- 왕복 필터는 언어별로 다르다: 한국어 생성 질문은 BM25로, 영어 생성 질문은 그 질문의 임베딩과 조각
  임베딩의 순위로 검색해 자기 섹션이 3위 안이면 남긴다. `questions.before`와 `questions.roundtrip_kept`가
  kind·언어별 필터 전후 수다(검색 줄은 answerable만, 자료 밖 신호는 필터 전 전체를 쓴다).
- 세 임베딩(질문·개발 질의·조각)의 모델 이름이 다르면 eval이 멈춘다. `skipped_blank`는 질의가 비어
  임베딩하지 않고 건너뛴 개발 행 수다.
- 로컬에서 확인한 기준값(API 없이): 정답 섹션이 있는 개발 97행 중 `bm25` 1위 83, `rule_a` 1위 92.
  `bm25`·`rule_a`는 영어 질의에도 BM25만 쓴다(규칙 A의 영어 임베딩 경로는 재현하지 않는다).
