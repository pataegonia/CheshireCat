"""레드팀 보조 세트(IN-08, 이슈 #62): draft(nano 초안) → review(사람이 고름) → run(서버에 보냄).

문항은 운영진 gpt-5-nano만 쓴다(TU-16 · IN-08). 이 패키지에는 꼴 설명(말)만 있고 문항 · 예시
발화는 없다. 서버는 이 패키지를 읽지 않는다. 순서와 명령은 README.md.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DRAFT_PATH = ROOT / "draft.jsonl"  # nano 초안(git 제외)
REVIEWED_PATH = ROOT / "reviewed.jsonl"  # 고른 기록: 줄마다 {"id", "decision"}(git 제외)
CASES_PATH = ROOT / "cases.jsonl"  # 남긴 문항(커밋 대상)
RUNS_DIR = ROOT.parent / "runs"  # run 결과(git 제외)
