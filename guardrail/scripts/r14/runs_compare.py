"""여러 실측(`runs/<이름>/summary.json`)의 반복 범위를 한 표로 내고 기준과 겹치는지 본다(API 0회).

실행(guardrail/에서):
`uv run python -X utf8 scripts/r14/runs_compare.py <기준 summary.json> <후보 ...>`
채택 기준(master-plan §10): 후보의 반복 범위가 기준의 범위와 겹치지 않고 높다.
닿아도 겹친 것으로 본다. 실행 하나에 설정이 여러 개면 각각 한 행이고 첫 파일의 첫 설정이 기준이다.
"""

import json
import sys
from pathlib import Path

# (표 머리, 점수 하나에서 값을 꺼내는 함수, 높을수록 좋은가)
METRICS = (
    ("가드 점수", lambda s: s["cv"]["guard_score"], True),
    ("block F1", lambda s: s["cv"]["block_f1"], True),
    ("범주 정확도", lambda s: s["cv"]["category_accuracy"], True),
    ("출력 평균", lambda s: s["cv"]["output"]["mean"], True),
    ("출력 실패율", lambda s: _fail(s, "output_judge"), False),
    ("입력 실패율", lambda s: _fail(s, "input_judge"), False),
    ("지연 p95(초)", lambda s: s["latency_ms"]["p95"] / 1000, False),
)


def _fail(score: dict, role: str) -> float:
    calls = score["llm_by_role"].get(role, {}).get("calls", 0)
    return score["errors"].get(role, 0) / calls if calls else 0.0


def ranges(scores: list[dict]) -> dict[str, tuple[float, float]]:
    """지표마다 (최솟값, 최댓값)."""
    out = {}
    for name, get, _ in METRICS:
        values = [get(s) for s in scores]
        out[name] = (min(values), max(values))
    return out


def verdict(base: tuple[float, float], cand: tuple[float, float], higher_is_better: bool) -> str:
    """후보 범위가 기준 범위와 겹치지 않을 때만 '나음'·'나쁨'. 닿으면 겹침."""
    if cand[0] > base[1]:
        return "나음" if higher_is_better else "나쁨"
    if cand[1] < base[0]:
        return "나쁨" if higher_is_better else "나음"
    return "겹침"


def load(path: str) -> list[tuple[str, int, dict[str, tuple[float, float]]]]:
    summary = json.loads(Path(path).read_text("utf-8"))
    return [(name, len(c["scores"]), ranges(c["scores"])) for name, c in summary["configs"].items()]


def main(paths: list[str]) -> None:
    rows = [row for p in paths for row in load(p)]
    base = rows[0][2]
    head = ["설정", "반복", *[m[0] for m in METRICS]]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for i, (name, n, r) in enumerate(rows):
        cells = [name, str(n)]
        for metric, _, better in METRICS:
            lo, hi = r[metric]
            cell = f"{lo:.3f}" if lo == hi else f"{lo:.3f}–{hi:.3f}"
            if i:
                cell += f" ({verdict(base[metric], r[metric], better)})"
            cells.append(cell)
        lines.append("| " + " | ".join(cells) + " |")
    sys.stdout.write("\n".join(lines) + "\n")
    if any(n < 3 for _, n, _ in rows):
        sys.stdout.write(
            "\n주의: 반복이 3회 미만인 행은 범위가 아니라 한두 값이다. 채택 판정에 쓰지 않는다.\n"
        )


def demo() -> None:
    """채택 규칙의 자가 점검(닿으면 겹침, 낮을수록 좋은 지표는 뒤집힌다)."""
    cases = (
        ((0.77, 0.80), (0.81, 0.85), True, "나음"),
        ((0.77, 0.80), (0.80, 0.85), True, "겹침"),
        ((0.77, 0.80), (0.70, 0.76), True, "나쁨"),
        ((0.03, 0.06), (0.00, 0.02), False, "나음"),
        ((0.03, 0.06), (0.07, 0.09), False, "나쁨"),
    )
    for base, cand, better, want in cases:
        if verdict(base, cand, better) != want:
            raise SystemExit(f"자가 점검 실패: {base} {cand} {better} → {want}")


if __name__ == "__main__":
    demo()
    main(sys.argv[1:])
