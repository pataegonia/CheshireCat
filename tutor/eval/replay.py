"""재생 측정(운영진 API 0회): 기준 실행 결과 JSON의 행별 action·인용 위에 규칙만 다시 적용한다.

판정 없는 채점은 action·인용만 보므로, 기준 실행이 낸 action·인용과 바꾼 행의 고정 응답으로
`run_local --responses`가 읽는 JSONL({id, response})을 만든다. 답 본문은 결과 JSON에 없어
바꾸지 않은 행의 answer는 자리표시("-")다. 판정 없는 채점에만 쓴다(판정 포함에는 쓰지 않는다).

변환
- delegation_override: 대행 신호(이 브랜치의 signals)가 있고 위기·지시 무력화 신호가 없으며
  낸 action이 answer·scaffold면 academic 고정 scaffold(인용 []).
- intent: T 의도 판정 기록(`harness.intent_probe` 결과 JSON, `--intent-records`)의 이름표로 서버와
  같은 `intent.decide`를 적용한다. 고정 문장 조합이면 그 고정 문장(인용 []), 아니면(answer_seeking
  scaffold) 낸 인용은 그대로 두고 action만 바꾼다. 기준 실행에서 주 호출이 실패한 행(row_trace
  main_call=failed)은 서버처럼 기본값 action 위에 고정 문장 조합만 덮는다. 신호는 서버와 같은
  길(parse_request)로 찾는다.
  서버가 그 뒤에 하는 가드 판정(judge_output)·근거 확인은 판정 없는 재생이라 보지 않는다.
- delegation_override+intent: 서버 순서대로 대행 덮기를 먼저, 그 결과 action 위에 T 덮기.
- none: 아무것도 바꾸지 않는다(기준 점수가 그대로 나오는지 확인용).

사용: python -m tutor.eval.replay <기준 결과 JSON> <cases JSONL> <out JSONL>
    [--run 0] [--transform delegation_override|intent|delegation_override+intent|none]
    [--intent-records intent-….json]
출력은 행 id·수만(본문 없음).
"""

import argparse
import json
import sys
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from tutor.answer.templates import TEXT, Fixed, fixed_body, template_for
from tutor.judge import intent
from tutor.judge.conversation import tidy
from tutor.judge.flow import _messages
from tutor.judge.signals import Signal, detect
from tutor.request import parse_request

PLACEHOLDER = "-"
Transform = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]


def request_signals(row: dict[str, Any]) -> tuple[Signal, ...]:
    """서버와 같은 길(parse_request → 대화 정리)로 찾은 신호. 요청을 못 읽으면 ()."""
    req = parse_request({"messages": row["messages"]})
    return detect(tidy(_messages(req))) if req is not None else ()


def delegation_override(row: dict[str, Any], emitted: dict[str, Any]) -> dict[str, Any] | None:
    signals = request_signals(row)
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
NEEDS_INTENT = ("intent", "delegation_override+intent")


def intent_transform(
    labels: Mapping[str, str | None],
    *,
    after_delegation: bool,
    failed: Collection[str] = (),
) -> Transform:
    """T 이름표(행 id → 이름표)로 덮는 변환. after_delegation이면 대행 덮기를 먼저 한다.

    failed는 기준 실행에서 주 호출이 실패한 행 id다(row_trace main_call). 서버는 그 행에서 기본값의
    action 위에 고정 문장 조합만 덮으므로 여기서도 그렇게 한다."""

    def fn(row: dict[str, Any], emitted: dict[str, Any]) -> dict[str, Any] | None:
        first = delegation_override(row, emitted) if after_delegation else None
        current = first if first is not None else emitted
        decided = intent.decide(labels.get(row["id"]), request_signals(row), current.get("action"))
        if decided is None:
            return first
        kind = template_for(decided.category, decided.action)
        if kind is not None:
            return dict(fixed_body(kind))
        if first is None and row["id"] in failed:
            return None  # 주 호출 실패 행: 고정 문장이 아닌 덮기는 하지 않는다
        cited = list(current.get("citations") if first is not None else emitted.get("cited") or [])
        return {"answer": PLACEHOLDER, "action": decided.action, "citations": cited}

    return fn


def load_intent_labels(path: Path) -> dict[str, str | None]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    return {r["id"]: r.get("label") for r in rows}


def failed_rows(base: Mapping[str, Any], run: int) -> set[str] | None:
    """기준 실행 run에서 주 호출이 실패한 행 id(row_trace main_call). row_trace가 없으면 None."""
    by_run = (base.get("row_trace") or {}).get("by_run")
    if not by_run or run >= len(by_run):
        return None
    return {rid for rid, entry in by_run[run].items() if entry.get("main_call") == "failed"}


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
    names = sorted([*TRANSFORMS, *NEEDS_INTENT])
    ap.add_argument("--transform", default="delegation_override", choices=names)
    ap.add_argument("--intent-records", type=Path, help="intent_probe 결과 JSON(intent 변환용)")
    args = ap.parse_args(argv)
    if args.transform in NEEDS_INTENT and args.intent_records is None:
        ap.error(f"--transform {args.transform}에는 --intent-records가 필요하다")
    base = json.loads(args.base.read_text(encoding="utf-8"))
    if args.transform in NEEDS_INTENT:
        labels = load_intent_labels(args.intent_records)
        failed = failed_rows(base, args.run)
        if failed is None:
            sys.stdout.write("row_trace 없음: 주 호출 실패 행을 모른다(기본값 action으로 본다)\n")
        after = args.transform != "intent"
        fn = intent_transform(labels, after_delegation=after, failed=failed or ())
    else:
        fn = TRANSFORMS[args.transform]
    emitted = base["report"]["cv_emitted_by_run"][args.run]
    lines = args.cases.read_text(encoding="utf-8").splitlines()
    rows = {r["id"]: r for r in (json.loads(x) for x in lines if x)}
    out, changed = replay(emitted, rows, fn)
    text = "\n".join(json.dumps(o, ensure_ascii=False) for o in out) + "\n"
    args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(f"행 {len(out)}개, 바꾼 행 {len(changed)}개\n")
    new_action = {o["id"]: (o["response"] or {}).get("action") for o in out}
    for row_id, cat, act in changed:
        sys.stdout.write(f"  {row_id} 정답 범주 {cat}, 기준 action {act} -> {new_action[row_id]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
