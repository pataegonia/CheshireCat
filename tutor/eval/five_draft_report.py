"""후보 R·T5·T5-H2·T5-R0 공개 선별 비교(`run_local --rows-jsonl` 결과만 읽는다, API 0회).

    python -m tutor.eval.five_draft_report R=runs/r.jsonl T5=runs/t5.jsonl [--base R]

팔마다 실행별 행 평균 점수(행 points 평균 × 100,
run_local의 범주 가중 튜터 점수와 다르다)·action 정확도, 유효 초안 수 분포, 초안 상태(유효·실패·
늦음), 단서 코드와 단서 초안이 골라진 수, top1 재작성 결과, 요청당 튜터 시도 수를 낸다. 초안 5개
팔은 앞 3개만으로 골랐을 때와 다섯으로 고른 action이 갈린 행을 정답 action과 견줘 고침·망침으로
센다(같은 요청 안의 반사실, 생성 반복 아님). 기준 팔과는 행 id별 실행 평균 points를 짝지어 차이
평균과 부트스트랩 95% 구간, 고침·망침(행 action 정답 다수결이 바뀐 수)을 낸다. 본문은 읽지 않는다.
"""

import argparse
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

BOOT = 2000


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _score(rows: Sequence[Mapping[str, Any]]) -> float:
    pts = [r["points"] for r in rows if r.get("points") is not None]
    return 100 * statistics.fmean(pts) if pts else float("nan")


def _hinted(rec: Mapping[str, Any]) -> set[int]:
    """단서 초안 번호(마지막 len(md_hints)개)."""
    n, k = rec.get("md_n") or 0, len(rec.get("md_hints") or [])
    return set(range(n - k, n))


def arm_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    runs: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        runs[r["run"]].append(r)
    scores = [_score(v) for _k, v in sorted(runs.items())]
    acc = [statistics.fmean(x["action_ok"] for x in v) for _k, v in sorted(runs.items())]
    states, valid, hints, redraft = Counter(), Counter(), Counter(), Counter()
    hint_pick = fix3 = break3 = split3 = 0
    for r in rows:
        if not r.get("md_n"):
            continue
        valid[r.get("md_ok")] += 1
        for code in r.get("md_codes") or []:
            states["v" if code == "-" else "l" if code == "cut" else "f"] += 1
        hints.update(r.get("md_hints") or [])
        if r.get("md_pick") is not None and r["md_pick"] in _hinted(r):
            hint_pick += 1
        redraft[r.get("md_redraft") or "none"] += 1
        p3, p5 = r.get("md_pick3_action"), r.get("md_pick_action")
        if r.get("md_valid3") is not None and p3 != p5:
            split3 += 1
            gold = r["gold_action"]
            fix3 += p5 == gold and p3 != gold
            break3 += p3 == gold and p5 != gold
    return {
        "runs": len(runs),
        "rows": len(rows),
        "score": scores,
        "action_acc": acc,
        "valid": dict(sorted(valid.items(), key=lambda kv: (kv[0] is None, kv[0]))),
        "states": dict(states),
        "hints": dict(hints),
        "hint_picked": hint_pick,
        "redraft": dict(redraft),
        "split3": split3,
        "fix3": fix3,
        "break3": break3,
    }


def _by_id(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r["id"]].append(r)
    return out


def paired(
    base: Sequence[Mapping[str, Any]], cand: Sequence[Mapping[str, Any]], seed: int = 0
) -> dict[str, Any]:
    """행 id별 실행 평균 points 차이(후보 − 기준)와 부트스트랩 95% 구간, action 고침·망침."""
    a, b = _by_id(base), _by_id(cand)
    ids = sorted(set(a) & set(b))
    if not ids:
        return {"n": 0}

    def mean_pts(rs: Sequence[Mapping[str, Any]]) -> float:
        pts = [r["points"] for r in rs if r.get("points") is not None]
        return statistics.fmean(pts) if pts else 0.0

    def ok(rs: Sequence[Mapping[str, Any]]) -> bool:
        return sum(r["action_ok"] for r in rs) * 2 > len(rs)

    diffs = [100 * (mean_pts(b[i]) - mean_pts(a[i])) for i in ids]
    rng = random.Random(seed)  # noqa: S311 - 재표집, 암호용 아님
    boots = sorted(statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(BOOT))
    return {
        "n": len(ids),
        "diff": statistics.fmean(diffs),
        "ci95": (boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT) - 1]),
        "fix": sum(ok(b[i]) and not ok(a[i]) for i in ids),
        "break": sum(ok(a[i]) and not ok(b[i]) for i in ids),
    }


def _fmt(xs: Sequence[float]) -> str:
    if not xs:
        return "-"
    mean = statistics.fmean(xs)
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return f"{mean:.2f} ± {sd:.2f} ({', '.join(f'{x:.2f}' for x in xs)})"


def report(arms: Mapping[str, Sequence[Mapping[str, Any]]], base: str) -> str:
    lines = [
        "| 팔 | 실행 | 행 | 행 평균 점수×100(범주 가중 아님) | action 정확도 | 유효 초안 수 | "
        "초안 v/f/l | 단서 코드 | "
        "단서 초안 골라짐 | top1 재작성 | 3표≠5표 (고침/망침) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in arms.items():
        s = arm_summary(rows)
        st = s["states"]
        lines.append(
            f"| {name} | {s['runs']} | {s['rows']} | {_fmt(s['score'])} | "
            f"{_fmt([100 * x for x in s['action_acc']])} | {s['valid']} | "
            f"{st.get('v', 0)}/{st.get('f', 0)}/{st.get('l', 0)} | {s['hints']} | "
            f"{s['hint_picked']} | {s['redraft']} | {s['split3']} ({s['fix3']}/{s['break3']}) |"
        )
    if base in arms:
        lines += ["", f"기준 {base} 대비(행 id별 실행 평균 points 차이 × 100, 부트스트랩 95%):"]
        for name, rows in arms.items():
            if name == base:
                continue
            p = paired(arms[base], rows)
            if not p["n"]:
                lines.append(f"- {name}: 겹치는 행 없음")
                continue
            lo, hi = p["ci95"]
            lines.append(
                f"- {name}: {p['n']}행, 차이 {p['diff']:+.2f} [{lo:+.2f}, {hi:+.2f}], "
                f"action 고침 {p['fix']} · 망침 {p['break']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("arms", nargs="+", metavar="이름=경로")
    ap.add_argument("--base", default="R")
    args = ap.parse_args(argv)
    arms: dict[str, list[dict[str, Any]]] = {}
    for item in args.arms:
        name, sep, path = item.partition("=")
        if not sep:
            ap.error(f"이름=경로 꼴이어야 한다: {item}")
        rows = arms.setdefault(name, [])
        offset = 1 + max((r["run"] for r in rows), default=-1)  # 파일마다 실행 번호를 잇는다
        rows.extend({**r, "run": r["run"] + offset} for r in load(Path(path)))
    sys.stdout.write(report(arms, args.base) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
