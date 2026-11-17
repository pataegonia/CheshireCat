"""레드팀 초안을 사람이 읽고 고른다(IN-08, 이슈 #62).

저장소 루트에서 `python -m harness.redteam.review`.

- 키 · 라벨 바꾸기는 튜터 점검 세트 도구(`tutor.eval.case_review.ask` · `KEYS`)를 그대로 쓴다:
  Enter 남김, n 버림, 숫자 action 바꿈, c 범주 바꿈, q 끝.
- 꼴(shapes.SHAPES 순서)마다 남길 수를 채울 때까지 그 꼴의 초안을 묻는다. 경로 · 목표 · 화면
  머리는 튜터 것(축별 목표, team_cases/ 경로)으로 박혀 있어 여기에 다시 쓴다.
- 화면에 꼴 이름 · 꼴 설명 · 기대 범주/action · 호출에 준 섹션 제목을 같이 보인다. missing_attr
  초안에는 튜터 검색(`tutor.retrieval.retrieve`)으로 찾은 상위 3개 섹션 ID도 보인다(대상이 교재에
  있고 그 속성이 없는지 사람이 확인하는 용도).
- 고를 때마다 바로 파일에 덧붙인다: 남긴 문항은 harness/redteam/cases.jsonl(커밋 대상, 행은
  case_set.problem을 통과해야 한다), 결정은 harness/redteam/reviewed.jsonl(git 제외). 끊겨도 다시
  실행하면 둘 중 하나에 있는 초안을 건너뛴다. 문항 본문은 이 화면에만 나온다.
"""

import argparse
import asyncio
import sys
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

from harness import redteam
from harness.redteam.draft import kept_by_shape
from harness.redteam.shapes import SHAPES, Shape, expected_actions, shape_of
from integration.actions import load_actions
from tutor.eval import case_set
from tutor.eval.case_review import KEYS, RULE, Context, Read, ask
from tutor.eval.grader import KB_DIR, score_local, scoring
from tutor.retrieval import retrieve

SEARCH_K = 3  # missing_attr 초안에 보이는 검색 상위 섹션 수
SEARCHED = ("missing_attr",)


def top_sections(text: str, k: int = SEARCH_K) -> list[str]:
    """튜터 검색(기본 색인, BM25)의 상위 k개 섹션 ID. 운영진 API는 부르지 않는다."""
    return [s.id for s in asyncio.run(retrieve(text, k=k))]


def _indent(text: str) -> str:
    return text.replace("\n", "\n      ")


def show(
    draft: Mapping[str, Any],
    shape: Shape,
    left: int,
    kept: Counter[str],
    ctx: Context,
    out: TextIO,
    search: Callable[[str], list[str]] = top_sections,
) -> None:
    """초안 하나: 꼴 · 꼴 설명 · 기대 범주/action · 준 섹션 제목(missing_attr는 검색 상위 3개 ID),
    대화, 초안 라벨."""
    out.write(f"{RULE}\n[{shape.name}] {draft['id']} · 이 꼴의 남은 초안 {left}개\n")
    out.write(f"남긴 수: 이 꼴 {kept[shape.name]}/{shape.keep}, 모두 {sum(kept.values())}개")
    out.write(f"/{sum(s.keep for s in SHAPES.values())}\n")
    out.write(f"꼴 설명: {shape.form}\n")
    actions = " 또는 ".join(expected_actions(shape, ctx.table))
    out.write(f"기대: 범주 {shape.category} → action {actions} (짝 {shape.twin})\n")
    meta = draft.get("draft") or {}
    sections = meta.get("sections", [])
    given = [f"{sid} {case_set.section_title(sid, ctx.kb[sid])}" for sid in sections]
    out.write(f"호출에 준 섹션: {' · '.join(given) if given else '없음'}\n")
    if shape.name in SEARCHED:
        asked = next(m["content"] for m in reversed(draft["messages"]) if m["role"] == "user")
        hits = search(asked)
        out.write(f"검색 상위 {SEARCH_K}개: {' · '.join(hits) if hits else '없음'}")
        out.write(" (대상이 교재에 있고 그 속성이 없는지 확인)\n")
    out.write("대화:\n")
    for number, m in enumerate(draft["messages"], 1):
        out.write(f"  [{number}] {m['role']}: {_indent(m['content'])}\n")
    labels = draft["labels"]
    out.write(f"초안 라벨: 범주 {labels['category']} → action {labels['action']}")
    evidence = labels.get("evidence") or []
    out.write(f" · 근거 {' · '.join(evidence) if evidence else '없음'}\n")
    numbers = " · ".join(f"{i} {a}" for i, a in enumerate(scoring.ACTIONS, 1))
    out.write(f"action 번호: {numbers}\n{KEYS}\n")


def review(
    drafts: Sequence[Mapping[str, Any]],
    *,
    done: set[str],
    kept: Counter[str],
    ctx: Context,
    read: Read,
    out: TextIO,
    search: Callable[[str], list[str]] = top_sections,
) -> Counter[str]:
    """꼴마다 남길 수를 채울 때까지 묻고 고른 결과를 파일에 덧붙인다. 꼴별 남긴 수를 돌려준다."""
    queues: dict[str, deque[Mapping[str, Any]]] = {name: deque() for name in SHAPES}
    for d in drafts:
        shape = shape_of(d["id"])
        if shape is not None and d["id"] not in done:
            queues[shape.name].append(d)
    try:
        for shape in SHAPES.values():
            queue = queues[shape.name]
            while kept[shape.name] < shape.keep and queue:
                draft = queue.popleft()
                show(draft, shape, len(queue), kept, ctx, out, search)
                answer = ask(draft, ctx, read, out)
                if answer is None:
                    raise EOFError
                decision, labels = answer
                if decision == "keep":
                    row = case_set.final_row(draft, labels)
                    if (field := case_set.problem(row, ctx.kb)) is not None:
                        out.write(f"남기지 못했다(형식: {field}). 버린 것으로 적는다\n")
                        decision = "drop"
                    else:  # 남긴 문항을 먼저 쓴다(끊기면 그 행으로 건너뛴다)
                        case_set.append_rows(redteam.CASES_PATH, [row])
                        kept[shape.name] += 1
                case_set.append_rows(
                    redteam.REVIEWED_PATH, [{"id": draft["id"], "decision": decision}]
                )
    except (EOFError, KeyboardInterrupt):
        out.write("\n")
    short = [s.name for s in SHAPES.values() if kept[s.name] < s.keep]
    out.write(f"{RULE}\n남긴 문항 {sum(kept.values())}개. 남길 수를 못 채운 꼴 {len(short)}개")
    out.write(f"{': ' + ', '.join(short) if short else ''}\n")
    if short:
        out.write("더 만들려면: python -m harness.redteam.draft --only-short\n")
    out.write(f"남긴 문항: {redteam.CASES_PATH}\n")
    return kept


def _utf8_stdio() -> None:
    """런박스 SSH · Windows 콘솔에서 한글이 깨지거나 인코딩 오류로 멈추지 않게 한다."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m harness.redteam.review", description="레드팀 초안 고르기(IN-08)"
    )
    ap.parse_args(argv)
    try:
        drafts = case_set.read_jsonl(redteam.DRAFT_PATH)
        reviewed = case_set.read_jsonl(redteam.REVIEWED_PATH)
        cases = case_set.read_jsonl(redteam.CASES_PATH)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    if not drafts:
        ap.error("초안이 없다: 먼저 python -m harness.redteam.draft --yes")
    _utf8_stdio()
    review(
        drafts,
        done={row["id"] for row in [*reviewed, *cases]},
        kept=kept_by_shape(cases),
        ctx=Context(score_local.load_kb_dir(KB_DIR), load_actions()),
        read=input,
        out=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
