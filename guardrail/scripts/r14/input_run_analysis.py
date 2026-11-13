"""R14 §4-1: 입력 단독 실측(`--input-only`)의 응답을 예시 구성별로 나눠 본다(API 0회).

실행: `uv run python -X utf8 scripts/r14/input_run_analysis.py runs/<name>/responses.jsonl` (guardrail/).
볼 것: ① block 정밀도·재현율·F1(가드·튜터 따로) ② 맨 끝(가장 비슷한) 예시가 정답과 같은 쪽인 행과
반대쪽인 행의 정확도 ③ nano가 kNN 다수·LR 둘 다와 엇갈린 행의 nano 정답률(R14-D6) ④ 경계 오판 목록.
"""

import json
import sys
from collections import Counter
from pathlib import Path

from guardrail import knn
from guardrail.config import load_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval import cv

root = Path(__file__).resolve().parents[2]
cfg = load_config(root / "configs" / "default.yaml")
BLOCK = frozenset(cfg.block_categories)
rows = labeled_rows(load_public(cfg.paths.public))
by_id = {r.id: r for r in rows}
split = cv.load_splits()
recs = [json.loads(x) for x in Path(sys.argv[1]).read_text("utf-8").splitlines()]

last = {}  # 행 id → 맨 끝 예시의 범주(하네스와 같은 폴드 학습 행으로 다시 계산)
for k in range(split.folds):
    idx = knn.KnnIndex(cv.train_rows(split, rows, fold=k), cfg)
    for r in rows:
        if split.fold.get(split.cluster[r.id]) == k:
            shots = idx.fewshot(r.text)
            last[r.id] = shots[-1].category if shots else None


def prf(items):
    tp = sum(g and p for g, p in items)
    fp = sum((not g) and p for g, p in items)
    fn = sum(g and (not p) for g, p in items)
    pr = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
    return f"P {pr:.3f} R {rc:.3f} F1 {f1:.3f} (tp {tp} fp {fp} fn {fn}, n {len(items)})"


def out(line=""):
    sys.stdout.write(line + "\n")


reps = sorted({r["repeat"] for r in recs})
for rep in reps:
    cur = [r for r in recs if r["repeat"] == rep and r["id"] in last]
    out(f"== repeat {rep}: {len(cur)}행, source {dict(Counter(r['source'] for r in cur))}")
    out(f"   오류 {dict(Counter(e for r in cur for e in r['errors']))}")
    sub, flips, disagree, cats = Counter(), [], [], Counter()
    for ep in ("guard", "tutor", None):
        items = []
        for r in cur:
            row = by_id[r["id"]]
            if ep and row.endpoint != ep:
                continue
            items.append((row.category in BLOCK, r["response"]["input"]["category"] in BLOCK))
        out(f"   block [{ep or '전체'}] {prf(items)}")
    for r in cur:
        row = by_id[r["id"]]
        gold, pred = row.category, r["response"]["input"]["category"]
        g, p = gold in BLOCK, pred in BLOCK
        same = (last[r["id"]] in BLOCK) == g
        key = ("block" if g else "allow", "끝=같은쪽" if same else "끝=반대쪽")
        sub[key, "n"] += 1
        sub[key, "ok"] += g == p
        if gold != "normal":
            cats["n"] += 1
            cats["ok"] += gold == pred
        if g != p:
            flips.append(f"{r['id']} {gold}>{pred} 끝예시={last[r['id']]}")
        k, lr = r.get("knn_major"), r.get("lr_pred")
        if k and lr and (k in BLOCK) != p and (lr in BLOCK) != p:
            disagree.append(g == p)
    for key in sorted({k for k, _ in sub}):
        out(f"   {key[0]} 행, {key[1]}: block 판정 정답 {sub[key, 'ok']}/{sub[key, 'n']}")
    out(f"   범주 정확도(non-normal) {cats['ok']}/{cats['n']}")
    out(f"   nano가 kNN·LR 둘 다와 엇갈린 행: {len(disagree)}개, nano 정답 {sum(disagree)}개")
    out(f"   경계 오판 {len(flips)}건:")
    for f in sorted(flips, key=lambda s: s.split()[1]):
        out("     " + f)
