# 대회 배포 자료

「안전벨트를 채운 AI 튜터」 경진대회 배포 자료를 런박스 `/home/elicer/competition`에서 그대로 옮겼다(2026-11-10). 기준 문서는 [SPEC.md](SPEC.md)다.

| 경로 | 내용 |
|---|---|
| `SPEC.md` | 참가자 규격 |
| `kb/` | 지식 베이스(파이썬·프롬프트·금융) |
| `public/` | 공개 개발 세트(`tutor.jsonl`, `guard.jsonl`, 라벨 포함) |
| `harness/`, `grader/` | 로컬 채점기 |
| `server.py` | 시작용 최소 서버 |
| `submission.json` | 제출 양식(빈 틀) |
| `kosa-competition-ot.pdf` | 참가자 오리엔테이션 자료 |

- 이 저장소는 비공개여야 한다. 대회 기간에는 팀 코드를 공개하지 않는다(SPEC §7).
- `.env`(API 키·`TEAM_TOKEN`)와 실제 토큰을 채운 `submission.json`은 커밋하지 않는다.
- 런박스의 원본이 바뀌면 다시 받아 덮어쓴다.
