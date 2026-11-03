# 무방비 베이스라인: 막힘 (2026-11-03)

이 폴더의 `*-fake.csv`·`*-fake.json`은 **실제 베이스라인 수치가 아니다.** 하네스가 무방비 설정으로 스모크 3종을 끝까지 돌고 리포트를 낸다는 것만 보여 준다(GP0 관문, `--fake`).

- 왜 막혔나: 엘리스 API 키·base URL·모델 목록을 아직 받지 못했다(master-plan §8). 이번 실행은 실제 API를 한 번도 부르지 않았다(`calls_total: 0`).
- `--fake`의 뜻: 이번 실행이 부르는 역할(generator·attack_judge)을 모두 `faults: fake_ok`로 덮는다. 생성은 첫 청크의 첫 문장을 `[S1]`로 인용한 한 문장이고, 판정기는 늘 "공격 실패·거절 아님"이다(`llm.py`의 `FAKE_OK`). 그래서 공격 성공률 0, 과잉 거절 0, 위기 템플릿·연락처 누락 30/30은 고정 응답이 만든 값이다. 어디에도 보고하지 않는다.
- 모든 그룹이 `claimable=False`다(씨앗 59개 미만, 스모크라서).
- 실제 베이스라인: 키가 오면 GP0 S0.7의 두 단계 명령(`--estimate`로 추정을 본 뒤 `--max-calls <추정 이상>`)으로 낸다. 예상 호출은 공격·정상 220회, 위기 99회다. 결과는 `results/<날짜>-baseline-smoke{,-crisis}.csv|json`으로 이 파일들을 대신한다.
- 추적: S0.7 이슈 #13 `guardrail:blocked`.

| 파일 | 데이터 | 반복 | 비고 |
|---|---|---|---|
| `2026-11-03-baseline-smoke-fake.csv`·`.json` | `smoke_attack.jsonl`(50, 로컬) + `smoke_benign.jsonl`(50) | 1 | 판정기 fake |
| `2026-11-03-baseline-smoke-crisis-fake.csv`·`.json` | `smoke_crisis.jsonl`(30, 로컬) | 3(pass^3) | 코드 채점 |
