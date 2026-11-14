"""저장된 교차 검증 실행의 가드 감점 위치(API 0회). 문항 원문은 출력하지 않는다.

실행(guardrail/에서): uv run python -X utf8 scripts/r14/error_breakdown.py runs/<a>/responses.jsonl [...]
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv
from guardrail.eval.run import _item
from guardrail.eval.score import scoring

cfg = load_config()
block = frozenset(cfg.block_categories)
split = cv.load_splits()
held = set(split.holdout)
pub = {r.id: r for r in load_public(cfg.paths.public) if r.endpoint == "guard"}
W_DEC, W_CAT = scoring.GUARD_DECISION_WEIGHT, scoring.GUARD_CATEGORY_WEIGHT
W_OUT = 1 - W_DEC - W_CAT

wrong_by_row: dict[str, int] = defaultdict(int)  # 행 → block 판정을 틀린 실행 수
pair_by_row: dict[str, str] = {}
types_total: Counter[str] = Counter()
tiers_total: Counter[str] = Counter()
runs = 0
for path in sys.argv[1:]:
    recs = [json.loads(x) for x in Path(path).read_text("utf-8").splitlines()]
    by_run: dict[tuple, list[dict]] = defaultdict(list)
    for rec in recs:
        by_run[(rec.get("config"), rec.get("repeat"))].append(rec)
    for (cname, rep), group in sorted(by_run.items(), key=lambda kv: str(kv[0])):
        runs += 1
        res, types, tiers = [], Counter(), Counter()
        out_loss = {"grounded": 0.0, "span": 0.0, "safe": 0.0}
        n_out = 0
        for rec in group:
            r = pub.get(rec["id"])
            if r is None or split.cluster[r.id] in held:
                continue  # 튜터 행·홀드아웃 행은 세지 않는다
            item, variant = _item(r)
            s = scoring.score_guard(item, variant, rec["response"])
            res.append(s)
            gold_cat = r.labels["category"]
            pred = rec["response"]["input"]
            if not s["input_correct"]:
                kind = "오탐" if s["false_block"] else "미탐"
                key = f"{kind} {gold_cat}->{pred['category']}"
                types[key] += 1
                wrong_by_row[r.id] += 1
                pair_by_row[r.id] = f"{gold_cat}->{pred['category']}"
            elif s["category_scored"] and not s["category_correct"]:
                types[f"범주만 {gold_cat}->{pred['category']}"] += 1
            if s["has_output"]:
                n_out += 1
                out_loss["grounded"] += 1 - float(s["output_correct"])
                out_loss["span"] += 1 - s["span_f1"]
                out_loss["safe"] += 1 - float(s["safe_correct"])
                if not s["output_correct"]:
                    tiers[
                        f"grounded 틀림 tier={rec.get('tier')} 정답={r.labels.get('grounded')}"
                    ] += 1
                elif s["span_f1"] < 1:
                    tiers[f"구간만 F1<1 tier={rec.get('tier')}"] += 1
                if not s["safe_correct"]:
                    tiers[f"safe 틀림 정답={r.labels.get('safe')}"] += 1
        g = scoring.guard_score(res)
        f1 = scoring.block_f1(res)
        cats = [x["category_correct"] for x in res if x["category_scored"]]
        cat = sum(cats) / len(cats)
        outs = [
            (float(x["output_correct"]) + x["span_f1"] + float(x["safe_correct"])) / 3
            for x in res
            if x["has_output"]
        ]
        om = sum(outs) / len(outs)
        print(
            f"\n== {Path(path).parent.name} {cname} r{rep}: 가드 {g} (행 {len(res)}, 출력 {n_out})"
        )
        print(
            f"  잃은 점수: block F1 {f1:.3f} → {100 * W_DEC * (1 - f1):.1f} · "
            f"범주 {cat:.3f} → {100 * W_CAT * (1 - cat):.1f} · "
            f"출력 {om:.3f} → {100 * W_OUT * (1 - om):.1f} "
            f"(grounded {100 * W_OUT * out_loss['grounded'] / n_out / 3:.1f}, "
            f"구간 {100 * W_OUT * out_loss['span'] / n_out / 3:.1f}, "
            f"safe {100 * W_OUT * out_loss['safe'] / n_out / 3:.1f})"
        )
        for k, v in types.most_common():
            print(f"  {v:3d} {k}")
        for k, v in tiers.most_common():
            print(f"  {v:3d} {k}")
        types_total.update(types)
        tiers_total.update(tiers)

print(f"\n== {runs}회 합계 (회당 평균)")
for k, v in [*types_total.most_common(), *tiers_total.most_common()]:
    print(f"  {v / runs:5.1f} {k}")
always = Counter(pair_by_row[i] for i, n in wrong_by_row.items() if n == runs)
some = sum(1 for n in wrong_by_row.values() if 0 < n < runs)
print(f"\nblock 판정을 {runs}회 모두 틀린 행 {sum(always.values())}개: {dict(always)}")
print(f"일부 회차만 틀린 행 {some}개")
