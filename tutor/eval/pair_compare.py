"""행 단위 짝 비교(운영진 API 0회): 기준 결과 JSON들과 변형 결과 JSON들의 행별 평균 점수를 비교한다.

각 결과 JSON의 `report.points`(행 → 실행마다 문항 점수)를 모아 행마다 평균을 내고, 변형 − 기준이
`--threshold`(기본 0.25) 이상이면 고친 행, 그 반대면 망친 행으로 센다. 정답 범주별로 나눠 보이고
순이득(고친 행 − 망친 행)을 낸다. 출력은 행 id·범주·수만(본문 없음).

사용: python -m tutor.eval.pair_compare --base A.json [B.json …] --variant C.json [D.json …]
    [--cases competition/public/tutor.jsonl] [--threshold 0.25] [--show 20]
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path


def row_means(paths: Sequence[Path]) -> dict[str, float]:
    """결과 JSON들의 행별 평균 점수(판정 오류 등으로 None인 실행은 뺀다)."""
    sums: dict[str, list[float]] = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for row_id, scores in data["report"]["points"].items():
            sums.setdefault(row_id, []).extend(s for s in scores if s is not None)
    return {row_id: sum(v) / len(v) for row_id, v in sums.items() if v}


def compare(
    base: dict[str, float], variant: dict[str, float], categories: dict[str, str], threshold: float
) -> dict[str, list[tuple[str, str, float]]]:
    """고친 행·망친 행 [(id, 범주, 변화)]. 두 쪽에 다 있는 행만 본다."""
    fixed, broken = [], []
    for row_id in sorted(set(base) & set(variant)):
        delta = variant[row_id] - base[row_id]
        item = (row_id, categories.get(row_id, "?"), delta)
        if delta >= threshold:
            fixed.append(item)
        elif delta <= -threshold:
            broken.append(item)
    return {"fixed": fixed, "broken": broken}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="행 단위 짝 비교(API 0회)")
    ap.add_argument("--base", type=Path, nargs="+", required=True)
    ap.add_argument("--variant", type=Path, nargs="+", required=True)
    ap.add_argument("--cases", type=Path, default=Path("competition/public/tutor.jsonl"))
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args(argv)
    lines = args.cases.read_text(encoding="utf-8").splitlines()
    categories = {
        r["id"]: r["labels"]["category"] for r in (json.loads(x) for x in lines if x.strip())
    }
    base, variant = row_means(args.base), row_means(args.variant)
    result = compare(base, variant, categories, args.threshold)
    out = sys.stdout
    shared = set(base) & set(variant)
    mean_b = sum(base[r] for r in shared) / max(1, len(shared))
    mean_v = sum(variant[r] for r in shared) / max(1, len(shared))
    out.write(
        f"행 {len(shared)}개 평균 {mean_b * 100:.2f} → {mean_v * 100:.2f} "
        f"(기준 {len(args.base)}개 파일, 변형 {len(args.variant)}개 파일)\n"
    )
    nf, nb = len(result["fixed"]), len(result["broken"])
    out.write(f"고친 행 {nf} · 망친 행 {nb} · 순이득 {nf - nb:+d} (문턱 {args.threshold})\n")
    by_cat: dict[str, Counter] = {}
    for kind in ("fixed", "broken"):
        for _row, cat, _d in result[kind]:
            by_cat.setdefault(cat, Counter())[kind] += 1
    for cat in sorted(by_cat):
        c = by_cat[cat]
        out.write(f"- {cat}: 고침 {c['fixed']} · 망침 {c['broken']}\n")
    for kind, label in (("fixed", "고친 행"), ("broken", "망친 행")):
        items = sorted(result[kind], key=lambda t: -abs(t[2]))[: args.show]
        out.write(f"{label}: " + " ".join(f"{r}({c},{d:+.2f})" for r, c, d in items) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
