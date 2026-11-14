"""R14: 입력 단독 실측 여러 회의 범위·잡음 바닥·오류 종류를 낸다(API 0회).

실행: `uv run python -X utf8 scripts/r14/input_runs_compare.py <1회차 responses.jsonl> <--repeat 2 responses.jsonl>` (guardrail/).
결과 문서 results/2026-11-13-input-v1-solo.md의 표를 다시 낸다. 분석 전용 스크립트다.
"""

import json, sys
from collections import Counter, defaultdict
from guardrail import knn
from guardrail.config import load_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval import cv
cfg = load_config("configs/default.yaml"); B = frozenset(cfg.block_categories)
rows = labeled_rows(load_public(cfg.paths.public)); by = {r.id: r for r in rows}
split = cv.load_splits()
runs = []
a = [json.loads(x) for x in open(sys.argv[1], encoding="utf-8")]
b = [json.loads(x) for x in open(sys.argv[2], encoding="utf-8")]
runs.append({r["id"]: r for r in a})
for rep in (0, 1):
    runs.append({r["id"]: r for r in b if r["repeat"] == rep})
last = {}
for k in range(split.folds):
    idx = knn.KnnIndex(cv.train_rows(split, rows, fold=k), cfg)
    for r in rows:
        if split.fold.get(split.cluster[r.id]) == k:
            last[r.id] = idx.fewshot(r.text)[-1].category
ids = sorted(runs[0])
def f1(items):
    tp = sum(g and p for g, p in items); fp = sum((not g) and p for g, p in items); fn = sum(g and not p for g, p in items)
    pr = tp/(tp+fp) if tp+fp else 0; rc = tp/(tp+fn) if tp+fn else 0
    return (2*pr*rc/(pr+rc) if pr+rc else 0, pr, rc, tp, fp, fn)
pred = lambda run, i: run[i]["response"]["input"]["category"]
for ep in ("guard", "tutor", "all"):
    sel = [i for i in ids if ep == "all" or by[i].endpoint == ep]
    out = []
    for run in runs:
        F, P, R, tp, fp, fn = f1([(by[i].category in B, pred(run, i) in B) for i in sel])
        nn = [i for i in sel if by[i].category != "normal"]
        cat = sum(pred(run, i) == by[i].category for i in nn) / len(nn)
        out.append(f"F1 {F:.3f} P {P:.3f} R {R:.3f} (tp{tp} fp{fp} fn{fn}) cat {cat:.3f}")
    print(ep, len(sel), " | ".join(out))
print("errors per run:", [dict(Counter(e for r in run.values() for e in r["errors"])) for run in runs])
# flips
fl_block = [i for i in ids if len({pred(run, i) in B for run in runs}) > 1]
fl_cat = [i for i in ids if len({pred(run, i) for run in runs}) > 1]
print(f"block 판정이 3회 중 갈린 행: {len(fl_block)}/{len(ids)} (가드 {sum(by[i].endpoint=='guard' for i in fl_block)})")
print(f"범주가 3회 중 갈린 행: {len(fl_cat)}/{len(ids)}")
wrong_n = Counter(sum((pred(run, i) in B) != (by[i].category in B) for run in runs) for i in ids)
print("block 오판 횟수별 행 수(0·1·2·3회):", [wrong_n[k] for k in range(4)])
g = [i for i in ids if by[i].endpoint == "guard"]
wg = Counter(sum((pred(run, i) in B) != (by[i].category in B) for run in runs) for i in g)
print("  가드만:", [wg[k] for k in range(4)])
# error classes per run
for name, cond in (("normal>jailbreak", lambda g_, p: g_ == "normal" and p == "jailbreak_injection"),
                   ("allow>block(전체)", lambda g_, p: g_ not in B and p in B),
                   ("harmful>pii", lambda g_, p: g_ == "harmful_content" and p == "pii_disclosure"),
                   ("off_topic>allow", lambda g_, p: g_ == "off_topic" and p not in B),
                   ("academic>allow", lambda g_, p: g_ == "academic_dishonesty" and p not in B),
                   ("harmful>allow", lambda g_, p: g_ == "harmful_content" and p not in B),
                   ("jailbreak>allow", lambda g_, p: g_ == "jailbreak_injection" and p not in B)):
    print(f"  {name}: {[sum(cond(by[i].category, pred(run, i)) for i in ids) for run in runs]}")
# categories pooled
gold = Counter(by[i].category for i in ids)
print("범주별 정답(3회):", {c: [sum(pred(run, i) == c for i in ids if by[i].category == c) for run in runs] + [gold[c]] for c in sorted(gold)})
# last-example subgroup pooled
sub = defaultdict(lambda: [0, 0])
for run in runs:
    for i in ids:
        gb = by[i].category in B
        key = ("block" if gb else "allow", "same" if (last[i] in B) == gb else "opp")
        sub[key][0] += (pred(run, i) in B) == gb; sub[key][1] += 1
print("맨 끝 예시별(3회 합산):", {k: f"{v[0]}/{v[1]} = {v[0]/v[1]:.3f}" for k, v in sorted(sub.items())})
per = defaultdict(list)
for run in runs:
    s2 = defaultdict(lambda: [0, 0])
    for i in ids:
        gb = by[i].category in B
        key = ("block" if gb else "allow", "same" if (last[i] in B) == gb else "opp")
        s2[key][0] += (pred(run, i) in B) == gb; s2[key][1] += 1
    for k, v in s2.items(): per[k].append(f"{v[0]}/{v[1]}")
print("  회별:", dict(sorted(per.items())))
# D6
d = []
for run in runs:
    n = ok = 0
    for i in ids:
        r = run[i]; p = pred(run, i) in B; k, lr = r.get("knn_major"), r.get("lr_pred")
        if r["source"] != "nano":
            continue
        if k and lr and (k in B) != p and (lr in B) != p:
            n += 1; ok += p == (by[i].category in B)
    d.append(f"{ok}/{n}")
print("nano가 kNN·LR 둘 다와 엇갈린 행의 nano 정답:", d)
st = [i for i in ids if all((pred(run, i) in B) != (by[i].category in B) for run in runs)]
print("3회 모두 틀린 행:", Counter(f"{by[i].category}>{pred(runs[0], i)}" for i in st))
