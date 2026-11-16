"""재생 측정(운영진 API 0회): 기준 실행 결과 JSON의 행별 action·인용 위에 규칙만 다시 적용한다.

판정 없는 채점은 action·인용만 보므로, 기준 실행이 낸 action·인용과 바꾼 행의 고정 응답으로
`run_local --responses`가 읽는 JSONL({id, response})을 만든다. 답 본문은 결과 JSON에 없어
바꾸지 않은 행의 answer는 자리표시("-")다. 판정 없는 채점에만 쓴다(판정 포함에는 쓰지 않는다).

변환
- delegation_override: 대행 신호(이 브랜치의 signals)가 있고 위기·지시 무력화 신호가 없으며
  낸 action이 answer·scaffold면 academic 고정 scaffold(인용 []).
- none: 아무것도 바꾸지 않는다(기준 점수가 그대로 나오는지 확인용).

사용: python -m tutor.eval.replay <기준 결과 JSON> <cases JSONL> <out JSONL>
    [--run 0] [--transform delegation_override|none]
출력은 행 id·수만(본문 없음).
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tutor.answer.templates import TEXT, Fixed
from tutor.judge.conversation import tidy
from tutor.judge.signals import Signal, detect

PLACEHOLDER = "-"
Transform = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]


def delegation_override(row: dict[str, Any], emitted: dict[str, Any]) -> dict[str, Any] | None:
    signals = detect(tidy(row["messages"]))
    if Signal.DELEGATION not in signals:
        return None
    if Signal.CRISIS in signals or Signal.OVERRIDE in signals:
        return None
    if emitted.get("action") not in ("answer", "scaffold"):
        return None
    return {"answer": TEXT[Fixed.SCAFFOLD_ACADEMIC], "action": "scaffold", "citations": []}


def no_change(row: dict[str, Any], emitted: dict[str, Any]) -> dict[str, Any] | None:
    return None


TRANSFORMS: dict[str, Transform] = {"delegation_override": delegation_override, "none": no_change}


def replay(
    emitted: dict[str, dict[str, Any]], rows: dict[str, dict[str, Any]], fn: Transform
) -> tuple[list[dict[str, Any]], list[tuple[str, str, Any]]]:
    """(응답 줄, 바꾼 행 [(id, 정답 범주, 기준 action)])."""
    out, changed = [], []
    for row_id, em in emitted.items():
        if row_id not in rows:
            continue
        new = fn(rows[row_id], em)
        if new is not None:
            changed.append((row_id, rows[row_id]["labels"]["category"], em.get("action")))
            resp: dict[str, Any] | None = new
        elif em.get("action") is None:
            resp = None  # 기준 실행에서 형식이 틀린 응답은 그대로 틀린 응답
        else:
            cited = list(em.get("cited") or [])
            resp = {"answer": PLACEHOLDER, "action": em["action"], "citations": cited}
        out.append({"id": row_id, "response": resp})
    return out, changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="기준 실행 위에 규칙만 다시 적용(API 0회)")
    ap.add_argument("base", type=Path)
    ap.add_argument("cases", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--run", type=int, default=0)
    ap.add_argument("--transform", default="delegation_override", choices=sorted(TRANSFORMS))
    args = ap.parse_args(argv)
    base = json.loads(args.base.read_text(encoding="utf-8"))
    emitted = base["report"]["cv_emitted_by_run"][args.run]
    lines = args.cases.read_text(encoding="utf-8").splitlines()
    rows = {r["id"]: r for r in (json.loads(x) for x in lines if x)}
    out, changed = replay(emitted, rows, TRANSFORMS[args.transform])
    text = "\n".join(json.dumps(o, ensure_ascii=False) for o in out) + "\n"
    args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(f"행 {len(out)}개, 바꾼 행 {len(changed)}개\n")
    for row_id, cat, act in changed:
        sys.stdout.write(f"  {row_id} 정답 범주 {cat}, 기준 action {act} -> scaffold(고정)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
