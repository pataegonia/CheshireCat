## 변경 내용

-

## 연결 이슈

Closes #

## 확인

- [ ] 트랙 라벨을 붙였다 (`guardrail` / `tutor` / `integration`, 우선순위 `P0`~`P2`)
- [ ] 로컬에서 lint·test를 돌렸다
- [ ] PR 본문과 커밋 메시지에 AI 도구 서명 문구가 없다
- [ ] 머지 전에 세 검사가 green이다: `guardrail / gate` · `integration / gate` · `pr-policy` (`.github/`를 바꾼 PR에는 `pr-policy / zizmor`가 하나 더 뜬다. 저장소 전체 비밀 키 검사는 `integration / gate` 안에서 돈다. 필수 검사 설정은 무료 비공개 저장소라 강제 불가 — 사람이 확인)
- [ ] base 브랜치를 바꿨다면 PR을 닫았다 다시 열었다(본문 수정으로는 테스트가 다시 돌지 않는다. 바꾸고 그대로 두면 `pr-policy`가 실패로 알린다)
