"""점검 세트 초안을 사람이 읽고 고른다(TU-16·TU-19, #80). `team_cases review`가 부른다.

- 초안(team_cases/draft.jsonl)을 남긴 수가 가장 적은 축부터 하나씩 보인다. 키: Enter 남김,
  n 버림, 숫자 action 바꿈, c 범주 바꿈, q 끝. 범주를 바꾸면 action과 근거를 표대로 다시 정한다.
- 고를 때마다 바로 파일에 덧붙인다: 남긴 문항은 점검 세트(team_cases/tutor.jsonl)에, 결정은 고른
  기록(team_cases/reviewed.jsonl, git 제외)에. 끊겨도 다시 실행하면 둘 중 하나에 있는 초안을
  건너뛰고 이어서 한다.
- 축마다 --min-per-axis개 이상, 모두 --total개 이상 남기면 저절로 끝난다.
- input()만 쓴다(SSH에서 그대로 된다). 문항 본문은 이 화면에만 나오고 로그로 남기지 않는다.
"""

import argparse
import sys
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple, TextIO

from integration.actions import ActionRule, load_actions
from tutor.eval import case_set
from tutor.eval.case_set import AXES
from tutor.eval.grader import KB_DIR, score_local, scoring

KEYS = "[Enter] 남김 · n 버림 · 1-7 action 바꿈 · c 범주 바꿈 · q 끝"
HANGUL_KEYS = {"ㅜ": "n", "ㅊ": "c", "ㅂ": "q"}  # 한글 입력 상태에서 같은 자리 키
RULE = "─" * 60
Read = Callable[[str], str]


class Goal(NamedTuple):
    per_axis: int
    total: int

    def reached(self, kept: Mapping[str, int]) -> bool:
        return all(kept.get(a, 0) >= self.per_axis for a in AXES) and (
            sum(kept.values()) >= self.total
        )


class Context(NamedTuple):
    kb: Mapping[str, str]
    table: Mapping[str, ActionRule]


def _next_axis(queues: Mapping[str, deque[Any]], kept: Mapping[str, int]) -> str | None:
    """남은 초안이 있는 축 중 남긴 수가 가장 적은 축(같으면 AXES 순서)."""
    left = [a for a in AXES if queues[a]]
    return min(left, key=lambda a: kept.get(a, 0)) if left else None


def _indent(text: str) -> str:
    return text.replace("\n", "\n      ")


def _show_labels(labels: Mapping[str, Any], by: str, ctx: Context, out: TextIO) -> None:
    rule = ctx.table[labels["category"]]
    table = " 또는 ".join(rule.actions)
    out.write(f"범주 {labels['category']} → action {labels['action']} ({by})")
    out.write(f" · 표의 action: {table}\n")
    evidence = labels.get("evidence") or []
    shown = [f"{sid} {case_set.section_title(sid, ctx.kb[sid])}" for sid in evidence]
    if not shown and case_set.label_evidence(["?"], rule.cite, labels["action"]):
        shown = ["없음(근거가 필요한 문항인데 없어 인용은 재지 않는다. 틀렸으면 버린다)"]
    out.write(f"근거: {' · '.join(shown) if shown else '없음'}\n")


def _show(
    draft: Mapping[str, Any], left: int, kept: Counter[str], goal: Goal, ctx: Context, out: TextIO
) -> None:
    out.write(f"{RULE}\n[{draft['axis']}] {draft['id']} · 남은 초안 {left}개\n")
    counts = ", ".join(f"{a} {kept[a]}" for a in AXES)
    out.write(f"남긴 수: {counts} (목표 축마다 {goal.per_axis}, 모두 {goal.total})\n")
    meta = draft.get("draft") or {}
    if meta.get("form"):
        asked = f" · 정해 준 범주 {meta['category']}" if meta.get("category") else ""
        out.write(f"꼴: {meta['form']}{asked}\n")
    out.write("대화:\n")
    for number, m in enumerate(draft["messages"], 1):
        out.write(f"  [{number}] {m['role']}: {_indent(m['content'])}\n")
    by = {"nano": "nano가 고름", "table": "표대로"}.get(meta.get("action_by"), "초안")
    _show_labels(draft["labels"], by, ctx, out)
    actions = " · ".join(f"{i} {a}" for i, a in enumerate(scoring.ACTIONS, 1))
    out.write(f"action 번호: {actions}\n{KEYS}\n")


def _relabel(
    draft: Mapping[str, Any], category: str, action: str, table: Mapping[str, ActionRule]
) -> dict[str, Any]:
    """바꾼 범주·action의 라벨. 근거는 초안 근거를 공개 세트 라벨 꼴로(case_set.label_evidence)."""
    proposed = (draft.get("draft") or {}).get("evidence", draft["labels"].get("evidence", []))
    evidence = case_set.label_evidence(proposed, table[category].cite, action)
    return {"action": action, "category": category, "evidence": evidence}


def _choose_category(read: Read, out: TextIO) -> str | None:
    out.write(" · ".join(f"{i} {c}" for i, c in enumerate(scoring.CATEGORIES, 1)) + "\n")
    key = read("범주 번호(Enter는 취소)> ").strip()
    if key.isdecimal() and 1 <= int(key) <= len(scoring.CATEGORIES):
        return scoring.CATEGORIES[int(key) - 1]
    if key:
        out.write("범주를 바꾸지 않았다\n")
    return None


def ask(
    draft: Mapping[str, Any], ctx: Context, read: Read, out: TextIO
) -> tuple[str, dict[str, Any]] | None:
    """('keep' 또는 'drop', 라벨). q면 None. 숫자·c로 라벨을 바꾸면 다시 묻는다."""
    labels = dict(draft["labels"])
    while True:
        key = read("> ").strip().lower()
        key = HANGUL_KEYS.get(key, key)
        if key == "":
            return "keep", labels
        if key == "n":
            return "drop", labels
        if key == "q":
            return None
        if key.isdecimal() and 1 <= int(key) <= len(scoring.ACTIONS):
            action = scoring.ACTIONS[int(key) - 1]
            labels = _relabel(draft, labels["category"], action, ctx.table)
            _show_labels(labels, "바꿈", ctx, out)
        elif key == "c":
            category = _choose_category(read, out)
            if category is not None:
                labels = _relabel(draft, category, ctx.table[category].action, ctx.table)
                _show_labels(labels, "범주를 바꿔 표대로", ctx, out)
        else:
            out.write(f"모르는 키다. {KEYS}\n")


def review(
    drafts: Sequence[Mapping[str, Any]],
    *,
    done: set[str],
    kept: Counter[str],
    goal: Goal,
    ctx: Context,
    read: Read,
    out: TextIO,
) -> Counter[str]:
    """고른 결과를 파일에 덧붙이며 목표까지 묻는다. 끝난 뒤의 축별 남긴 수를 돌려준다."""
    queues = {a: deque(d for d in drafts if d["axis"] == a and d["id"] not in done) for a in AXES}
    try:
        while not goal.reached(kept):
            axis = _next_axis(queues, kept)
            if axis is None:
                short = [a for a in AXES if kept[a] < goal.per_axis] or list(AXES)
                out.write("고를 초안이 더 없다. 더 만들려면: python -m tutor.eval.team_cases")
                out.write(" generate " + " ".join(f"--axis {a}" for a in short) + "\n")
                break
            draft = queues[axis].popleft()
            _show(draft, sum(map(len, queues.values())), kept, goal, ctx, out)
            answer = ask(draft, ctx, read, out)
            if answer is None:
                break
            decision, labels = answer
            if decision == "keep":  # 점검 세트를 먼저 쓴다(끊기면 그 행으로 건너뛴다)
                case_set.append_rows(case_set.FINAL_PATH, [case_set.final_row(draft, labels)])
                kept[axis] += 1
            case_set.append_rows(
                case_set.REVIEWED_PATH, [{"id": draft["id"], "decision": decision}]
            )
        else:
            out.write("목표를 채웠다.\n")
    except (EOFError, KeyboardInterrupt):
        out.write("\n")
    out.write(f"{RULE}\n남긴 수: " + ", ".join(f"{a} {kept[a]}" for a in AXES))
    out.write(f", 모두 {sum(kept.values())}개. 점검 세트: {case_set.FINAL_PATH}\n")
    return kept


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--min-per-axis",
        type=int,
        default=case_set.MIN_PER_AXIS,
        help=f"축마다 남길 최소 수(기본 {case_set.MIN_PER_AXIS})",
    )
    p.add_argument(
        "--total",
        type=int,
        default=case_set.TARGET_TOTAL,
        help=f"모두 남길 수(기본 {case_set.TARGET_TOTAL})",
    )


def _utf8_stdio() -> None:
    """런박스 SSH·Windows 콘솔 어디서나 한글이 깨지거나 인코딩 오류로 멈추지 않게 한다."""
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "replace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=errors)


def run(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.min_per_axis < 0 or args.total < 0:
        ap.error("--min-per-axis·--total은 0 이상이어야 한다")
    try:
        drafts = case_set.read_jsonl(case_set.DRAFT_PATH)
        reviewed = case_set.read_jsonl(case_set.REVIEWED_PATH)
        final = case_set.read_jsonl(case_set.FINAL_PATH)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    if not drafts:
        ap.error("초안이 없다: 먼저 python -m tutor.eval.team_cases generate --yes")
    _utf8_stdio()
    ctx = Context(score_local.load_kb_dir(KB_DIR), load_actions())
    review(
        drafts,
        done={row["id"] for row in [*reviewed, *final]},
        kept=Counter(row.get("axis") for row in final),
        goal=Goal(args.min_per_axis, args.total),
        ctx=ctx,
        read=input,
        out=sys.stdout,
    )
    return 0
