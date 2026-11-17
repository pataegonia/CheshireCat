"""동결 후보 실행의 행 점검(측정 전용, API 0회).

저장소 루트에서 `python -m harness.freeze_rows A.json …`.

run_local 결과 JSON(report.cv_emitted_by_run · row_trace)을 여러 개 받아 실행을 모두 합쳐 본다.
1) T 이름표(row_trace intent)별로 낸 action과 응함(complied) 수, 응함 행 id.
2) normal 사실 손실 행: action이 answer인데 사실 점수가 1 미만인 실행 수와, 그 실행 중 정답 근거
   섹션을 인용한 수·충실 실패 수.
행 id·라벨·수만 낸다(본문 없음).
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from tutor.eval.run_local import public_tutor_rows

SKIP_LABELS = (None, "none", "학습 질문")  # T가 없거나 실패했거나 덮지 않는 이름표


def load_runs(paths: Sequence[Path]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(행 → 낸 것, 행 → row_trace) 실행 목록."""
    runs = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        emitted = data["report"]["cv_emitted_by_run"]
        traces = (data.get("row_trace") or {}).get("by_run")
        if not traces or len(traces) != len(emitted):
            raise ValueError(f"{path.name}: row_trace가 없거나 실행 수가 다르다")
        runs.extend(zip(emitted, traces, strict=True))
    return runs


def intent_table(runs, gold: Mapping[str, Mapping[str, Any]], out: TextIO) -> None:
    seen: Counter[tuple[str, str]] = Counter()
    complied: Counter[tuple[str, str]] = Counter()
    rows: dict[str, list[str]] = defaultdict(list)
    for emitted, trace in runs:
        for rid, em in emitted.items():
            label = trace.get(rid, {}).get("intent")
            if label in SKIP_LABELS or rid not in gold:
                continue
            key = (label, str(em.get("action")))
            seen[key] += 1
            if em.get("complied"):
                complied[key] += 1
                g = gold[rid]
                rows[f"{label}→{key[1]}"].append(f"{rid}({g['category']}/{g['action']})")
    out.write(f"T 이름표 → 낸 action (행×실행 {len(runs)}회 합, 응함 수):\n")
    for key in sorted(seen):
        out.write(f"- {key[0]} → {key[1]}: {seen[key]} · 응함 {complied[key]}\n")
    for name, ids in sorted(rows.items()):
        out.write(f"  응함 {name}: {' '.join(sorted(ids))}\n")


def facts_rows(runs, gold: Mapping[str, Mapping[str, Any]], out: TextIO) -> None:
    lost: dict[str, list[tuple[float, bool, bool]]] = defaultdict(list)
    answered: Counter[str] = Counter()
    for emitted, _trace in runs:
        for rid, em in emitted.items():
            g = gold.get(rid)
            if g is None or g["category"] != "normal" or em.get("action") != "answer":
                continue
            if em.get("facts") is None:
                continue
            answered[rid] += 1
            if em["facts"] < 1:
                cited_gold = bool(set(em.get("cited") or ()) & set(g.get("evidence") or ()))
                lost[rid].append((em["facts"], cited_gold, em.get("faithful") is False))
    n = len(runs)
    total = sum(1 - f for runs_ in lost.values() for f, _c, _u in runs_)
    out.write(f"normal 사실 손실 행 {len(lost)}개(실행 {n}회, 잃은 사실 합 {total:.2f}행분):\n")
    order = sorted(lost, key=lambda r: (-len(lost[r]), r))
    for rid in order:
        items = lost[rid]
        mean = sum(f for f, _c, _u in items) / len(items)
        cited = sum(c for _f, c, _u in items)
        unfaithful = sum(u for _f, _c, u in items)
        out.write(
            f"- {rid}: 잃음 {len(items)}/{answered[rid]} · 평균 사실 {mean:.2f}"
            f" · 정답 근거 인용 {cited} · 충실 실패 {unfaithful}\n"
        )
    most = [r for r in order if len(lost[r]) * 2 > answered[r] and answered[r] >= 2]
    out.write(f"answer 실행의 절반 넘게 잃은 행 {len(most)}개: {' '.join(most)}\n")


def self_check_counts(runs, out: TextIO) -> None:
    """2차 자체 검증 사유(row_trace self_check) 수. 켠 실행이 없으면 아무것도 쓰지 않는다."""
    seen = Counter(
        trace.get(rid, {}).get("self_check") for emitted, trace in runs for rid in emitted
    )
    seen.pop(None, None)
    if seen:
        out.write("2차 자체 검증(행×실행): " + ", ".join(f"{k} {v}" for k, v in seen.most_common()))
        out.write("\n")


def cite_kind(cited: Sequence[str], gold: Sequence[str]) -> tuple[str, float]:
    """(인용 꼴, 인용 F1).

    꼴: exact · extra(정답 + 다른 섹션) · partial(정답 일부) · wrong · none.
    """
    c, g = set(cited), set(gold)
    if not c:
        return "none", 0.0
    hit = len(c & g)
    if hit == 0:
        return "wrong", 0.0
    f1 = 2 * hit / (len(c) + len(g))
    if c == g:
        return "exact", f1
    return ("extra" if g <= c else "partial"), f1


def citation_rows(runs, gold: Mapping[str, Mapping[str, Any]], out: TextIO) -> None:
    """정답 근거가 있는 행에서 인용 꼴별 행×실행 수와 잃은 F1 합(1 − F1), 자주 틀린 행."""
    kinds: Counter[str] = Counter()
    lost: Counter[str] = Counter()
    bad: Counter[str] = Counter()
    for emitted, _trace in runs:
        for rid, em in emitted.items():
            g = gold.get(rid)
            if g is None or not g.get("evidence") or em.get("cited") is None:
                continue
            kind, f1 = cite_kind(em["cited"], g["evidence"])
            kinds[kind] += 1
            lost[kind] += 1 - f1
            if kind in ("wrong", "none"):
                bad[f"{rid}({g['category']}/{g['action']})"] += 1
    out.write(f"인용 꼴(정답 근거 있는 행×실행 {sum(kinds.values())}): ")
    out.write(", ".join(f"{k} {kinds[k]}(잃은 F1 {lost[k]:.1f})" for k in sorted(kinds)) + "\n")
    worst = [f"{r} {n}" for r, n in bad.most_common() if n * 2 > len(runs)]
    out.write(f"인용이 절반 넘게 틀리거나 없는 행 {len(worst)}개: {' · '.join(worst)}\n")


def action_rows(runs, gold: Mapping[str, Mapping[str, Any]], out: TextIO) -> None:
    """정답 action별로 낸 action 분포(행×실행)와, 절반 넘게 틀린 행."""
    by_gold: dict[str, Counter[str]] = defaultdict(Counter)
    wrong: Counter[str] = Counter()
    for emitted, _trace in runs:
        for rid, em in emitted.items():
            g = gold.get(rid)
            if g is None:
                continue
            got = str(em.get("action"))
            by_gold[f"{g['category']}/{g['action']}"][got] += 1
            if got != g["action"]:
                wrong[f"{rid}({g['category']}/{g['action']})"] += 1
    out.write("정답 → 낸 action(행×실행):\n")
    for key in sorted(by_gold):
        out.write(f"- {key}: " + ", ".join(f"{a} {n}" for a, n in by_gold[key].most_common()))
        out.write("\n")
    worst = [f"{r} {n}" for r, n in wrong.most_common() if n * 2 > len(runs)]
    out.write(f"action이 절반 넘게 틀린 행 {len(worst)}개: {' · '.join(worst)}\n")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="동결 후보 실행의 T·normal 사실 행 점검(API 0회)")
    ap.add_argument("runs", nargs="+", type=Path, help="run_local 결과 JSON(row_trace 포함)")
    ap.add_argument("--more", action="store_true", help="인용 꼴·action 분포도 낸다")
    args = ap.parse_args(argv)
    try:
        runs = load_runs(args.runs)
    except (OSError, ValueError, KeyError) as exc:
        ap.error(f"읽지 못했다({type(exc).__name__}: {exc})")
    gold = {row["id"]: row["labels"] for row in public_tutor_rows()}
    intent_table(runs, gold, sys.stdout)
    facts_rows(runs, gold, sys.stdout)
    self_check_counts(runs, sys.stdout)
    if args.more:
        citation_rows(runs, gold, sys.stdout)
        action_rows(runs, gold, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
