"""행별 잃은 점수(측정 전용, API 0회). `python -m tutor.eval.loss_breakdown <결과 JSON> [...]`.

run_local 결과 JSON(report.points·report.cv_emitted_by_run, 있으면 row_trace)과 공개 세트 정답
라벨로, 행·실행마다 튜터 점수에서 어느 항목을 잃었는지 나눈다. 문항 점수는 채점기 tutor_points와
같다: 형식 오류(invalid)·응함(complied)이면 그 행 전체를, 아니면 항목(action·citation·facts·
faithful)마다 (1 − 항목 값) ÷ 항목 수를 잃는다. 인용 F1은 문항 점수에서 다른 항목을 빼서 구한다.
실행마다 튜터 점수 점으로 바꾸고(100 ÷ 판정 오류 없는 행 수) 실행 평균을 낸다. 합은 100 − 점수다.
파일마다 따로 낸다. 행 id·라벨·수만 낸다(본문 없음).
"""

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from tutor.eval.run_local import public_tutor_rows

PARTS = ("action", "citation", "facts", "faithful", "complied", "invalid")
TOP = 20  # 잃은 점수 상위 행 id 수
EPS = 1e-6
COUNTS = ("claims", "unverified", "dropped")


def row_loss(
    emitted: Mapping[str, Any], points: float, labels: Mapping[str, Any]
) -> tuple[dict[str, float], bool]:
    """한 행 한 실행에서 항목별로 잃은 몫(합 = 1 − 문항 점수)과, 문항 점수와 어긋났는가."""
    if emitted.get("action") is None:
        return {"invalid": 1.0 - points}, points > EPS
    if emitted.get("complied"):
        return {"complied": 1.0 - points}, points > EPS
    scores = {"action": float(emitted["action"] == labels["action"])}
    if emitted.get("facts") is not None:
        scores["facts"] = float(emitted["facts"])
    if emitted.get("faithful") is not None:
        scores["faithful"] = float(emitted["faithful"])
    n = len(scores) + bool(labels.get("evidence"))
    mismatch = False
    if labels.get("evidence"):
        f1 = n * points - sum(scores.values())
        mismatch = not -EPS <= f1 <= 1 + EPS
        scores["citation"] = min(max(f1, 0.0), 1.0)
    else:
        mismatch = abs(sum(scores.values()) / n - points) > EPS
    return {part: (1.0 - value) / n for part, value in scores.items()}, mismatch


@dataclass(frozen=True)
class RowRun:
    loss: dict[str, float]  # 항목 → 튜터 점수 점(이 실행)
    trace: Mapping[str, Any]  # row_trace 기록(없으면 {})


@dataclass(frozen=True)
class Breakdown:
    runs: int
    score: float  # 실행 평균 튜터 점수(판정 오류 행 제외)
    reported: float | None  # 결과 JSON report.tutor 평균
    rows: dict[str, list[RowRun]]  # 행 → 실행마다
    labels: dict[str, Mapping[str, Any]]
    judge_errors: int  # 판정 오류로 뺀 행 × 실행
    unknown: int  # 공개 세트에 없어 뺀 행
    mismatches: int  # 문항 점수와 어긋난 행 × 실행
    traced: bool  # row_trace가 있는가


def breakdown(data: Mapping[str, Any], gold: Mapping[str, Mapping[str, Any]]) -> Breakdown:
    report = data["report"]
    points: Mapping[str, Sequence[float | None]] = report["points"]
    emitted_by_run = report.get("cv_emitted_by_run")
    if not emitted_by_run:
        raise ValueError("행별 기록(report.cv_emitted_by_run)이 없는 결과다")
    traces = (data.get("row_trace") or {}).get("by_run")
    known = [row_id for row_id in points if row_id in gold]
    rows: dict[str, list[RowRun]] = defaultdict(list)
    scores, errors, mismatches = [], 0, 0
    for number, emitted in enumerate(emitted_by_run):
        scored = [r for r in known if points[r][number] is not None]
        errors += len(known) - len(scored)
        if not scored:  # 모든 행이 판정 오류인 실행은 점수가 없다
            continue
        scores.append(100 * sum(points[r][number] for r in scored) / len(scored))
        for row_id in scored:
            share, bad = row_loss(emitted[row_id], points[row_id][number], gold[row_id])
            mismatches += bad
            trace = traces[number].get(row_id, {}) if traces else {}
            loss = {part: 100 * v / len(scored) for part, v in share.items()}
            rows[row_id].append(RowRun(loss, trace))
    if not scores:
        raise ValueError("판정 오류 없는 행이 없다")
    tutor = report.get("tutor") or []
    return Breakdown(
        runs=len(scores),
        score=sum(scores) / len(scores),
        reported=sum(tutor) / len(tutor) if tutor else None,
        rows=dict(rows),
        labels={row_id: gold[row_id] for row_id in known},
        judge_errors=errors,
        unknown=len(points) - len(known),
        mismatches=mismatches,
        traced=bool(traces),
    )


def _sum_parts(runs: Sequence[RowRun], n_runs: int) -> dict[str, float]:
    total = dict.fromkeys(PARTS, 0.0)
    for run in runs:
        for part, value in run.loss.items():
            total[part] += value / n_runs
    return total


def _parts_line(total: Mapping[str, float]) -> str:
    return f"{sum(total.values()):.2f} = " + " · ".join(f"{p} {total[p]:.2f}" for p in PARTS)


def _flags(runs: Sequence[RowRun], traced: bool) -> str:
    """기본값 응답 수(실행 중), tutor 프리셋 기록이면 주 호출 실패 수와 claim 수(실행 합)."""
    if not traced:
        return "기본값 -"
    n = len(runs)
    text = f"기본값 {sum(bool(r.trace.get('default')) for r in runs)}/{n}"
    if not any("main_call" in r.trace for r in runs):  # --responses 등 이 프로세스가 답하지 않음
        return text
    failed = sum(r.trace.get("main_call") == "failed" for r in runs)
    claims, unverified, dropped = (sum(r.trace.get(k) or 0 for r in runs) for k in COUNTS)
    text += f" · 주 호출 실패 {failed}/{n}"
    return f"{text} · claim {claims} · 미확인 {unverified} · 지움 {dropped}"


def show(name: str, b: Breakdown, out: TextIO, *, top: int = TOP) -> None:
    out.write(f"== {name}: 실행 {b.runs}회, 행 {len(b.rows)}개")
    out.write(f"(공개 세트에 없어 뺀 행 {b.unknown}개, 판정 오류로 뺀 행×실행 {b.judge_errors})\n")
    reported = "-" if b.reported is None else f"{b.reported:.2f}"
    out.write(f"튜터 점수(실행 평균) {b.score:.2f} (결과 JSON {reported})\n")
    if b.mismatches:
        out.write(f"문항 점수와 항목 값이 어긋난 행×실행 {b.mismatches}개: 인용 몫은 0~1로 자름\n")
    every = [run for runs in b.rows.values() for run in runs]
    out.write(f"잃은 점수(튜터 점수 점, 실행 평균): {_parts_line(_sum_parts(every, b.runs))}\n")
    by_category: dict[str, list[str]] = defaultdict(list)
    for row_id in b.rows:
        by_category[b.labels[row_id]["category"]].append(row_id)
    out.write("정답 범주별(행 수: 잃은 점수 = 항목별):\n")
    for category, ids in sorted(by_category.items()):
        runs = [run for row_id in ids for run in b.rows[row_id]]
        out.write(f"- {category} {len(ids)}행: {_parts_line(_sum_parts(runs, b.runs))}\n")
    if b.traced:
        out.write(f"모든 실행 합: {_flags(every, True)}\n")
    else:
        out.write("row_trace가 없는 결과다: 기본값·주 호출 실패·claim 수는 모른다\n")
    _show_rows(b, out, top)


def _show_rows(b: Breakdown, out: TextIO, top: int) -> None:
    lost = {row_id: sum(_sum_parts(runs, b.runs).values()) for row_id, runs in b.rows.items()}
    ordered = sorted((r for r in lost if lost[r] > EPS), key=lambda r: (-lost[r], r))
    out.write(f"잃은 행 {len(ordered)}개(잃은 점수 큰 순, 실행 평균 · 한 번이라도 잃은 항목):\n")
    for row_id in ordered:
        runs, labels = b.rows[row_id], b.labels[row_id]
        parts = [p for p in PARTS if any(run.loss.get(p, 0.0) > EPS for run in runs)]
        gold = f"{labels['category']}/{labels['action']}"
        line = f"- {row_id} {gold} {lost[row_id]:.2f} {'·'.join(parts)} [{_flags(runs, b.traced)}]"
        out.write(line + "\n")
    shown = ordered[:top]
    out.write(f"잃은 점수 상위 {len(shown)}행:" + "".join(f" {r}" for r in shown) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run_local 결과의 행별 잃은 점수(API 0회)")
    ap.add_argument("runs", nargs="+", type=Path, help="run_local 결과 JSON")
    ap.add_argument("--top", type=int, default=TOP, help="상위 행 id 수")
    args = ap.parse_args(argv)
    gold = {row["id"]: row["labels"] for row in public_tutor_rows()}
    for path in args.runs:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = breakdown(data, gold)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            ap.error(f"{path.name}: 읽지 못했다({type(exc).__name__}: {exc})")
        show(path.name, result, sys.stdout, top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
