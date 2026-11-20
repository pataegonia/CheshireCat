# 튜터 검색 신호의 자료 밖 분리력·겹침과 질문 색인 1위 상한을 잰다. API 호출 0회.
"""자료 밖 결정적 신호 분리력·겹침·1위 상한 (LLM·임베딩 호출 0회, 개발 160행만)."""
import itertools, json, math, os, random, sys
from collections import Counter, defaultdict
from pathlib import Path

for k in list(os.environ):  # 키가 있어도 쓰지 못하게
    if k.startswith(("OPENAI", "EMBEDDING")):
        del os.environ[k]
WT = Path(r"C:\Users\kirin\Desktop\CheshireCat\.claude\worktrees\score90-base")
sys.path[:0] = [str(WT), str(WT / "guardrail" / "src")]
os.chdir(WT)
OUT = Path(__file__).parent

import guardrail, tutor
from guardrail.eval.cv import load_splits
from tutor.judge.conversation import tidy
from tutor.retrieval import default_index
from tutor.retrieval.embedding import is_english
from tutor.retrieval.kb import bigrams

lines = []
def P(*a):
    s = " ".join(str(x) for x in a); print(s); lines.append(s)

assert Path(tutor.__file__).is_relative_to(WT) and Path(guardrail.__file__).is_relative_to(WT)
split = load_splits()
held = set(split.holdout)
raw = [json.loads(l) for l in (WT / "competition/public/tutor.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
rows = [r for r in raw if split.cluster[r["id"]] not in held]  # 홀드아웃은 id만 보고 버린다
del raw
idx = default_index()
N = len(idx.chunks)
cat = lambda r: r["labels"]["category"]
P(f"dev {len(rows)} sections {len(idx.sections)} chunks {N}", dict(Counter(cat(r) for r in rows)))
assert len(rows) == 160 and len(idx.sections) == 48 and N == 244
post = idx._bm25._postings  # 읽기만
idf = lambda g: math.log(1 + (N - len(post.get(g, ())) + 0.5) / (len(post.get(g, ())) + 0.5))

def qtext(r, all_roles=False):
    q, h = tidy(r["messages"]).search_query(all_roles=all_roles)
    return "\n".join([*h, q])[-idx.max_query_chars:], q

def maxrun(gs):
    best = cur = 0
    for g in gs:
        cur = 0 if g in post else cur + 1
        best = max(best, cur)
    return best

def unseen(gs):
    d = list(dict.fromkeys(gs))
    if not d: return 0.0, 0.0
    w = [idf(g) for g in d]
    return sum(g not in post for g in d) / len(d), sum(x for g, x in zip(d, w) if g not in post) / sum(w)

feat = {}
for r in rows:
    text, target = qtext(r)
    ranked = idx.search(text)
    t1 = ranked[0][1] if ranked else 0.0
    t2 = ranked[1][1] if len(ranked) > 1 else 0.0
    gq, gt = bigrams(text), bigrams(target)
    nd = max(1, len(set(gq)))
    tr = idx.search(target)
    tt1 = tr[0][1] if tr else 0.0
    uq, uqi = unseen(gq); ut, uti = unseen(gt)
    feat[r["id"]] = dict(
        # 방향은 미리 정함: 값이 클수록 자료 밖
        neg_top1=-t1, neg_margin=-(t1 - t2), neg_top1_per_len=-t1 / nd,
        unseen_ratio=uq, unseen_idf=uqi, maxrun=maxrun(gq),
        tgt_neg_top1_per_len=-tt1 / max(1, len(set(gt))), tgt_unseen_ratio=ut, tgt_unseen_idf=uti, tgt_maxrun=maxrun(gt),
        len_short=-nd,  # 기준선: 질의 길이만
    )
    r["_top"] = [s for s, _ in ranked[:5]]
    ra = idx.search(qtext(r, True)[0])
    r["_topA"] = [s for s, _ in ra[:5]]
    r["_en"] = is_english(target)
    ev = r["labels"].get("evidence") or []
    r["_subj"] = (ev[0] if ev else (r["_top"][0] if r["_top"] else "?")).split("-")[0]
    r["_fold"] = split.fold[split.cluster[r["id"]]]
SIG = list(next(iter(feat.values())))
oom = [r for r in rows if cat(r) == "out_of_material"]
nor = [r for r in rows if cat(r) == "normal"]
ans_gold = [r for r in rows if r["labels"]["action"] == "answer"]
assert len(oom) == 16 and len(nor) == 79
P("폴드별 자료밖/정상:", {f: (sum(r["_fold"] == f for r in oom), sum(r["_fold"] == f for r in nor)) for f in sorted({r["_fold"] for r in rows})})

def auc(p, n):
    return sum((a > b) + 0.5 * (a == b) for a in p for b in n) / (len(p) * len(n)) if p and n else float("nan")
def auc_ci(p, n, B=2000):
    rng = random.Random(13)
    v = sorted(auc(rng.choices(p, k=len(p)), rng.choices(n, k=len(n))) for _ in range(B))
    return v[int(.025 * B)], v[int(.975 * B)]
def wilson(k, n, z=1.96):
    if not n: return (0, 0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0, (c - h) / d), min(1, (c + h) / d)

def thr_single(s, train_nor, budget=0.05):
    v = sorted((feat[r["id"]][s] for r in train_nor), reverse=True)
    return v[int(budget * len(v))]  # 이 값보다 커야 발화 → 학습 정상 오탐 ≤ budget
def rule_single(s):
    def fit(tr_o, tr_n):
        t = thr_single(s, tr_n)
        return lambda r: feat[r["id"]][s] > t
    return fit
def rule_combo(a, b, op):
    def fit(tr_o, tr_n):
        k5 = int(0.05 * len(tr_n)); best = None
        ca = sorted({feat[r["id"]][a] for r in tr_n}, reverse=True)[:16]
        cb = sorted({feat[r["id"]][b] for r in tr_n}, reverse=True)[:16]
        if op == "AND":  # AND는 느슨한 문턱도 후보로
            ca = sorted({feat[r["id"]][a] for r in tr_n}, reverse=True)[::3]
            cb = sorted({feat[r["id"]][b] for r in tr_n}, reverse=True)[::3]
        for ta, tb in itertools.product(ca, cb):
            f = (lambda r, ta=ta, tb=tb: feat[r["id"]][a] > ta and feat[r["id"]][b] > tb) if op == "AND" else \
                (lambda r, ta=ta, tb=tb: feat[r["id"]][a] > ta or feat[r["id"]][b] > tb)
            fp = sum(map(f, tr_n))
            if fp > k5: continue
            key = (sum(map(f, tr_o)), -fp)
            if best is None or key > best[0]: best = (key, f)
        return best[1]
    return fit

folds = sorted({r["_fold"] for r in rows})
def cv_fire(fit):
    fired = {}
    for f in folds:
        g = fit([r for r in oom if r["_fold"] != f], [r for r in nor if r["_fold"] != f])
        for r in rows:
            if r["_fold"] == f: fired[r["id"]] = bool(g(r))
    return fired
def insample(fit):
    g = fit(oom, nor); return {r["id"]: bool(g(r)) for r in rows}

RULES = {s: rule_single(s) for s in SIG}
COMBOS = [("neg_top1_per_len", "unseen_idf", "AND"), ("neg_top1_per_len", "tgt_maxrun", "OR"), ("tgt_neg_top1_per_len", "tgt_unseen_idf", "AND"), ("neg_top1", "tgt_unseen_ratio", "OR")]
for a, b, op in COMBOS: RULES[f"{a} {op} {b}"] = rule_combo(a, b, op)

P("\n== (1) 신호 표: AUC[부트스트랩95%] | 표본내 잡음/16 (정상오탐/79) | 5겹 잡음/16 [Wilson] (정상오탐/79) | 5겹: 정답answer 전체 오탐/n")
res = {}
for name, fit in RULES.items():
    cv, ins = cv_fire(fit), insample(fit)
    line = f"{name:42s}"
    if name in SIG:
        p = [feat[r['id']][name] for r in oom]; n = [feat[r['id']][name] for r in nor]
        lo, hi = auc_ci(p, n); line += f" AUC {auc(p, n):.3f} [{lo:.2f},{hi:.2f}]"
    else: line += " AUC   -              "
    c = sum(cv[r["id"]] for r in oom); fp = sum(cv[r["id"]] for r in nor); fa = sum(cv[r["id"]] for r in ans_gold)
    lo, hi = wilson(c, 16)
    line += f" | in {sum(ins[r['id']] for r in oom)}/16 ({sum(ins[r['id']] for r in nor)}/79) | cv {c}/16 [{lo:.2f},{hi:.2f}] ({fp}/79) | ans {fa}/{len(ans_gold)}"
    P(line); res[name] = (c, -fp, cv)

best = max((k for k in res if k != "len_short"), key=lambda k: res[k][:2])
P("\n가장 좋은 규칙(5겹 잡음 수, 동률이면 오탐 적은 쪽; 15개 중 고른 것이라 낙관적):", best)
bcv = res[best][2]

P("\n== 과목·언어별 AUC (자료밖 n / 정상 n). 자료밖 행의 과목은 BM25 1위 섹션의 과목(추정)")
groups = {"한국어": lambda r: not r["_en"], "영어": lambda r: r["_en"], "PY": lambda r: r["_subj"] == "PY", "PR": lambda r: r["_subj"] == "PR", "FIN": lambda r: r["_subj"] == "FIN"}
show = [s for s in ("neg_top1", "neg_top1_per_len", "unseen_idf", "tgt_unseen_idf", "tgt_maxrun", "len_short")]
for gname, g in groups.items():
    o = [r for r in oom if g(r)]; n = [r for r in nor if g(r)]
    aucs = " ".join("%s %.2f" % (s, auc([feat[r["id"]][s] for r in o], [feat[r["id"]][s] for r in n])) for s in show)
    P("%-6s oom %2d nor %2d | %s | best규칙 5겹 잡음 %d/%d 오탐 %d/%d" % (
        gname, len(o), len(n), aucs, sum(bcv[r["id"]] for r in o), len(o), sum(bcv[r["id"]] for r in n), len(n)))

P("\n== (2) 겹침 (문서 7.4·7.12·7.14의 id, 5겹 발화)")
G = {
    "자료밖 매번 answer(5)": "3c276614 6a6fab48 1fb88fb9 66b4408c bb7dd589",
    "자료밖 가끔 answer(2)": "c7a51701 ff707d29",
    "자료밖 E가 고침(5)": "3112da0a eaf140a7 c89ae8a4 280d221d 72e82954",
    "자료밖 스스로 abstain(4)": "a430bf58 982a4741 e2dd7ca4 a95923af",
    "정상 매번 거절(2)": "e257f1f8 b03e3532",
    "정상 매번 0.25점(2)": "4be25e36 214d91ad",
}
byid = {r["id"]: r for r in rows}
named = set()
for g, ids in G.items():
    ids = ["pub-" + i for i in ids.split()]
    assert all(i in byid for i in ids), g
    named |= set(ids)
    for nm in dict.fromkeys([best, "neg_top1", "neg_top1_per_len", "tgt_unseen_idf", "tgt_maxrun"]):
        cv = res[nm][2]
        P("%-22s %-36s 발화 %d/%d: %s" % (g, nm, sum(cv[i] for i in ids), len(ids), [i[4:] for i in ids if cv[i]]))
P("문서의 16행이 자료밖 전체와 같은가:", {r["id"] for r in oom} <= named)
fp_rows = [r for r in rows if bcv[r["id"]] and cat(r) != "out_of_material"]
P("가장 좋은 규칙이 발화한 자료밖 아닌 행(5겹):", [(r["id"][4:], cat(r), r["labels"]["action"], "en" if r["_en"] else "ko") for r in fp_rows])
P("범주별 발화 수(5겹):", dict(Counter(cat(r) for r in rows if bcv[r["id"]])))

P("\n== (3) BM25 1위 빗나감 (정답 섹션 있는 개발 행)")
ev = [r for r in rows if r["labels"].get("evidence")]
hit = lambda r, key: bool(r[key]) and r[key][0] in r["labels"]["evidence"]
miss = [r for r in ev if not hit(r, "_top")]
nA = sum(hit(r, "_topA") for r in ev)
P("n=%d 기준 질의 1위 %d/%d = %.3f; 규칙A(튜터 발화 포함) 1위 %d/%d = %.3f" % (
    len(ev), len(ev) - len(miss), len(ev), (len(ev) - len(miss)) / len(ev), nA, len(ev), nA / len(ev)))
for r in miss:
    rank = next((i + 1 for i, s in enumerate(r["_top"]) if s in r["labels"]["evidence"]), ">5")
    P("  %s %-16s %s %-3s 정답 %s 1위 %s 정답순위 %s 규칙A적중 %s" % (
        r["id"], cat(r), "en" if r["_en"] else "ko", r["_subj"], ",".join(r["labels"]["evidence"]),
        r["_top"][0] if r["_top"] else "-", rank, hit(r, "_topA")))
P("빗나간 행 범주:", dict(Counter(cat(r) for r in miss)), "언어:", dict(Counter("en" if r["_en"] else "ko" for r in miss)),
  "과목:", dict(Counter(r["_subj"] for r in miss)))
P("evidence 행 언어:", dict(Counter("en" if r["_en"] else "ko" for r in ev)))
P("규칙A에서도 빗나간 행:", [(r["id"][4:], cat(r), "en" if r["_en"] else "ko") for r in ev if not hit(r, "_topA")])
P("규칙A가 새로 망친 행:", [r["id"][4:] for r in ev if hit(r, "_top") and not hit(r, "_topA")])
n, h0, hA = len(ev), (len(ev) - len(miss)) / len(ev), nA / len(ev)
up = lambda a, b, gain: 100 * (n / 160) * 0.25 * (b - a) * gain
P("상한(문서식) = 100 x (%d/160) x 1/4 x (0.95 - %.3f) x (0.93 - 0.21) = %.2f점" % (n, h0, up(h0, 0.95, 0.72)))
P("21%%의 Wilson 구간 10~40%%를 넣으면 %.2f ~ %.2f점" % (up(h0, 0.95, 0.93 - 0.40), up(h0, 0.95, 0.93 - 0.10)))
P("규칙A 뒤 잔여: 0.95까지 %.2f점, 1.00까지 %.2f점 (F1 차를 1.0으로 잡아도 %.2f점)" % (
    up(hA, 0.95, 0.72), up(hA, 1.0, 0.72), up(hA, 1.0, 1.0)))
P("evidence 행 정답 action:", dict(Counter(r["labels"]["action"] for r in ev)),
  "evidence 개수:", dict(Counter(len(r["labels"]["evidence"]) for r in ev)))
P("처음 훑어볼 때 화면에 찍힌 pub-62e10a10이 개발 행인가:", "pub-62e10a10" in byid)

(OUT / "results.txt").write_text("\n".join(lines), encoding="utf-8")
(OUT / "rows.json").write_text(json.dumps(
    [{"id": r["id"], "category": cat(r), "fold": r["_fold"], "en": r["_en"], "subj": r["_subj"],
      "best_cv_fire": bcv[r["id"]], **feat[r["id"]]} for r in rows], ensure_ascii=False, indent=0), encoding="utf-8")
