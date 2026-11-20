# 1차가 jailbreak인 행에 요청 유형(s-f-act)을 대입해 탈옥 재확인 효과를 잰다. API 호출 0회.
"""jailbreak 거부권 시뮬레이션 (API 0회, 개발 160행만). 실행: python -X utf8 veto.py <저장소 루트>
overlap.py의 읽기·분할·F1 부분을 재사용. 출력은 stdout(veto_out.txt에 덧붙임)."""
import json, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(sys.argv[1]); WT = ROOT / ".claude/worktrees/score90-base"; RUNS = ROOT / "guardrail/runs"
sp = json.loads((WT / "guardrail/src/guardrail/eval/splits/v1.json").read_text("utf-8"))
HELD, CL = set(sp["holdout"]), sp["cluster"]
m = re.search(r"^block_categories:\s*\[(.*?)\]", (WT / "guardrail/configs/default.yaml").read_text("utf-8"), re.M)
B = {x.strip() for x in m.group(1).split(",")}
gold = {}
for line in (WT / "competition/public/guard.jsonl").read_text("utf-8").splitlines():
    if line.strip():
        d = json.loads(line)
        if CL[d["id"]] not in HELD:  # 홀드아웃은 라벨도 읽지 않는다
            gold[d["id"]] = d["labels"]["category"]
IDS = sorted(gold); assert len(IDS) == 160
GB = {i: gold[i] in B for i in IDS}

def load(name):
    recs = defaultdict(dict)
    for line in (RUNS / name / "responses.jsonl").read_text("utf-8").splitlines():
        r = json.loads(line)
        if r["id"] in gold: recs[r["repeat"]][r["id"]] = r
    return recs
def f1(pred):
    tp = sum(GB[i] and pred[i] for i in IDS); fp = sum((not GB[i]) and pred[i] for i in IDS); fn = sum(GB[i] and not pred[i] for i in IDS)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0

act = {}
for i, r in load("s-f-act")[0].items():
    a = [x["parsed"]["act"] for x in r["llm"] if x["role"] == "input_judge" and x.get("parsed")]
    act[i] = a[0] if a else None
LISTS = {"L0 본목록": {"learning_question", "studies_risky_topic"}, "L_narrow": {"studies_risky_topic"},
         "L_wide": {"learning_question", "studies_risky_topic", "asks_value_not_in_material", "only_final_answer"}}
RUNSET = ["v1-input-1113", "v1-input-1113-r2", "s29-adopted", "s29-merged", "s29-default"]
rows = {k: [] for k in LISTS}; detail = []
for name in RUNSET:
    for rep, rr in sorted(load(name).items()):
        assert set(rr) == set(IDS)
        base = {i: rr[i]["response"]["input"]["category"] in B for i in IDS}
        jb = [i for i in IDS if rr[i]["response"]["input"]["category"] == "jailbreak_injection"]
        for ln, L in LISTS.items():
            rel = [i for i in jb if act[i] in L]
            new = {i: (base[i] and i not in rel) for i in IDS}
            fpj = [i for i in jb if gold[i] == "normal"]; tj = [i for i in jb if gold[i] == "jailbreak_injection"]
            oth = [i for i in jb if gold[i] not in ("normal", "jailbreak_injection")]
            # 정답이 다른 비차단 범주(예: pii)인데 jailbreak로 막은 행은 풀면 정답 쪽 -> 별도 집계
            oth_blk = [i for i in oth if GB[i]]; oth_nb = [i for i in oth if not GB[i]]
            R = dict(run=f"{name}/r{rep}", a=len(jb), noact=sum(act[i] is None for i in jb), fp=len(fpj), fp_rel=sum(i in rel for i in fpj),
                     tj=len(tj), tj_rel=[i[4:12] for i in tj if i in rel], oth_blk=len(oth_blk), oth_blk_rel=[i[4:12] for i in oth_blk if i in rel],
                     oth_nb=len(oth_nb), oth_nb_rel=sum(i in rel for i in oth_nb),
                     f1b=f1(base), f1a=f1(new), eb=sum(base[i] != GB[i] for i in IDS), ea=sum(new[i] != GB[i] for i in IDS),
                     fp_rel_ids=[i[4:12] for i in fpj if i in rel], fp_unrel_ids=[i[4:12] for i in fpj if i not in rel])
            R["net"] = R["fp_rel"] - len(R["tj_rel"]) - len(R["oth_blk_rel"])
            rows[ln].append(R)
def rng(x): return f"{st.mean(x):.2f} [{min(x)}-{max(x)}]" if all(isinstance(v, int) for v in x) else f"{st.mean(x):.4f} [{min(x):.4f}-{max(x):.4f}]"
for ln in LISTS:
    print(f"\n== 목록 {ln}: {sorted(LISTS[ln])}")
    print("반복 | (a)1차jb | act없음 | (b)정답normal 오탐 → 푼 | (c)정답jb | 잘못 푼 id | (d)정답 다른 차단범주→푼 id | (정답 비차단 비normal→푼) | F1 전→후 | 오답 전→후 | 순이득")
    for R in rows[ln]:
        print(f"{R['run']:20s} {R['a']:2d} {R['noact']:2d} | {R['fp']:2d}→{R['fp_rel']:2d} | {R['tj']:2d} {R['tj_rel']} | {len(R['oth_blk_rel'])} {R['oth_blk_rel']} | {R['oth_nb_rel']}/{R['oth_nb']} | {R['f1b']:.4f}→{R['f1a']:.4f} | {R['eb']}→{R['ea']} | {R['net']:+d}")
    for k, lab in [("a", "(a)1차jb"), ("fp", "정답normal"), ("fp_rel", "(b)푼 오탐"), ("tj", "정답jb"), ("oth_blk_rel", None), ("f1b", "F1전"), ("f1a", "F1후"), ("eb", "오답전"), ("ea", "오답후"), ("net", "순이득")]:
        x = [len(R[k]) if isinstance(R[k], list) else R[k] for R in rows[ln]]
        print(f"  평균[범위] {lab or '(d)다른차단범주 잘못 푼 수'}: {rng(x)}")
    print("  잘못 푼 진짜 탈옥 수:", rng([len(R['tj_rel']) for R in rows[ln]]))
print("\n== 4번: s-f-act의 act (L0 기준 푼 오탐/잃은 탈옥 행별)")
fpids = sorted({i for i in IDS if gold[i] == "normal"}); tjids = sorted({i for i in IDS if gold[i] == "jailbreak_injection"})
for ln in ["L0 본목록"]:
    rl = rows[ln]
    seen = set()
    for R in rl: seen |= {x for x in R["fp_rel_ids"] + R["tj_rel"] + R["fp_unrel_ids"] + R["oth_blk_rel"]}
for i in IDS:
    if i[4:12] in seen:
        print(i[4:12], gold[i], "act =", act[i])
