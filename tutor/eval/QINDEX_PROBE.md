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

`gen`은 `--yes` 없이 돌리면 예상 호출 수만 낸다. 먼저 `--limit 3 --yes`로 몇 조각만 해 보고
`gen_errors.jsonl`을 확인하는 것을 권한다. 같은 명령을 다시 돌리면 끝난 조각은 건너뛰고 실패한 조각만 한다.

## 예상 호출 수와 시간 (어림)

| 단계 | 호출 | 시간 |
|---|---|---|
| gen | nano 244회(조각당 1회, 실패하면 1회 더 보내 최대 488회), 동시 8개 | 10~20분 |
| embed | 질문 약 4,400문장(244 × 18)을 64개씩 약 69회 | 1~3분 |
| eval | 개발 질의 320개(160행 × 질의 2종)를 64개씩 5회. 캐시가 있으면 0회 | 1분 안쪽 |

모든 단계가 부르기 전에 예상 호출 수를 `config/tutor.yaml`의 `eval.max_calls`와 견준다. nano 429가 20회
이어지면 남은 호출을 보내지 않고 종료 코드 6으로 끝난다(run_local과 같은 규칙).

## 출력 파일 (`<폴더>/`)

| 파일 | 내용 |
|---|---|
| `questions.jsonl` | 줄마다 `chunk_id`(`<섹션 ID>#<섹션 안 순서>`)·`section_id`·`subject`·`kind`(answerable/unanswerable)·`lang`(ko/en)·`question`·`prompt_version` |
| `gen_errors.jsonl` | 실패한 조각: `chunk_id`·`error`(LLM 오류 코드 또는 `count:`·`empty:`·`lang:` + 칸 이름)·`finish_reason`(`length`면 출력 상한 2000토큰에 잘림) |
| `emb.npy`·`emb_meta.json` | 질문 임베딩(float32, questions.jsonl 줄 순서)과 순서 메타·questions.jsonl의 sha256. gen을 더 돌렸으면 embed를 다시 돌린다(eval이 맞지 않으면 멈춘다) |
| `dev_query_emb.npy`·`.json` | 개발 질의 임베딩 캐시(행 id·질의 종류·질의 해시만, 본문 없음) |
| `eval_report.json` | 검색 적중표와 자료 밖 신호표(`--report`로 경로를 바꾼다) |
| `eval_rows.jsonl` | 행 id별 신호값·문턱 판정·각 방식의 1위 섹션(질문 본문 없음) |

## 보고서 읽는 법

- `retrieval.<방식>.<그룹>` = `n`·`hit1`·`hit5`. 방식: `bm25`(지금 검색 질의), `rule_a`(규칙 A 질의의 BM25),
  `bm25_or_rule_a`(둘 중 하나라도 맞음), `qindex`(질문 색인 단독), `rrf`(BM25 + 질문 색인),
  `qindex_roundtrip`·`rrf_roundtrip`(왕복 필터 적용). `_last`가 붙으면 질의가 마지막 발화만이다.
  그룹: `all`, `ko`/`en`(마지막 발화 언어), `PY`/`PR`/`FIN`(정답 섹션 과목).
- `oom.<질의>.<신호>` = `auc`(out_of_material 16행 대 normal 79행)·`ci95`(묶음 단위 부트스트랩)·
  `caught`/`of`(5겹 문턱으로 잡은 자료 밖 행)·`flagged_answer`(잘못 잡은 정답 answer 행)·`flagged`(잡은 모든 행).
  `direction`이 -1인 신호(`s_pos`)는 부호를 뒤집어 "클수록 자료 밖"으로 맞춘 뒤 잰 값이다.
- 왕복 필터는 BM25로 걸러서 영어 질문이 거의 다 빠질 수 있다. `questions.roundtrip_kept`의 언어별 수를 먼저 본다.
- 로컬에서 확인한 기준값(API 없이): 정답 섹션이 있는 개발 97행 중 `bm25` 1위 83, `rule_a` 1위 92.
  이 도구의 `bm25`는 영어 질의에도 BM25만 쓴다(서버의 `english_embedding` 경로는 재현하지 않는다).
