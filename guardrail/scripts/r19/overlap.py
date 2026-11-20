# 가드 차단 결정의 행 단위 오답 겹침·oracle·2-of-3(판정기 묶기 확인). API 호출 0회.
"""가드 차단 결정의 행 단위 오답 겹침·oracle·결합 규칙 (API 0회, 개발 행만).

실행: python -X utf8 overlap.py <저장소 루트> [--texts]
홀드아웃 묶음 행은 id로 먼저 걸러 라벨·본문을 쓰지 않는다.
"""
import itertools as it
import json
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(sys.argv[1])
WT = ROOT / ".claude/worktrees/score90-base"
RUNS = ROOT / "guardrail/runs"
sp = json.loads((WT / "guardrail/src/guardrail/eval/splits/v1.json").read_text("utf-8"))
HELD, CL = set(sp["holdout"]), sp["cluster"]
m = re.search(r"^block_categories:\s*\[(.*?)\]", (WT / "guardrail/configs/default.yaml").read_text("utf-8"), re.M)
B = {x.strip() for x in m.group(1).split(",")}
gold, text = {}, {}
for line in (WT / "competition/public/guard.jsonl").read_text("utf-8").splitlines():
    if not line.strip():
        continue
    d = json.loads(line)
    if CL[d["id"]] in HELD:  # 홀드아웃: 라벨·본문을 보지 않는다
        continue
    gold[d["id"]] = d["labels"]["category"]
    text[d["id"]] = [x["content"] for x in d["messages"] if x["role"] == "user"][-1]
IDS = sorted(gold)
assert len(IDS) == 160, len(IDS)
GB = {i: gold[i] in B for i in IDS}

# 실행 → 프롬프트 계열(문서 2026-11-13-input-screening-1.md, 2026-11-13-v1.md에서 읽음)
FAMILY = {
    "v1-input-1113": "v1+예시(입력단독)", "v1-input-1113-r2": "v1+예시(입력단독)",
    "s-a-nofewshot": "v1 예시없음(입력단독)", "nofewshot-r2": "v1 예시없음(입력단독)",
    "s-b-v2a": "v2a+예시", "s-c-v2a-first": "v2a+예시(가까운것 앞)", "s-d-sublinear": "v1+예시(sublinear 검색)",
    "s-e-v2a-nofewshot": "v2a 예시없음", "s-f-act": "act(요청유형 14개)", "s-g-medium": "v1+예시 effort=medium",
    "s28-smoke": "전체 default(v1+예시)", "s29-default": "전체 default(v1+예시)",
    "s29-baseline": "전체 baseline(토글 끔)", "s29-combo": "전체 combo(v1 예시없음)",
    "s29-adopted": "전체 6.0.2(v1+예시)", "s29-merged": "전체 6.0.2(v1+예시)", "s29-r10": "전체 6.0.2(v1+예시)",
}
members = {}  # (run, repeat) -> {id: pred category}
meta = {}
print("== (1) 실행 표")
for d in sorted(RUNS.iterdir()):
    s = json.loads((d / "summary.json").read_text("utf-8"))
    (cname, c), = s["configs"].items()
    recs = defaultdict(dict)
    n = 0
    src = defaultdict(Counter)
    for line in (d / "responses.jsonl").read_text("utf-8").splitlines():
        r = json.loads(line)
        n += 1
        if r["id"] in gold:  # 가드 개발 행만
            recs[r["repeat"]][r["id"]] = r
            src[r["repeat"]][(r["source"], bool(r["errors"]))] += 1
    kind = "출력단독" if s.get("output_only") else "입력단독" if s.get("input_only") else "전체"
    f1s = [round(x["cv"]["block_f1"], 4) for x in c["scores"]]
    print(f"{d.name:18s} hash {c['config_hash']} {kind} 반복 {s['repeat']} 줄 {n} (가드 개발 {len(recs[0])}/회) "
          f"F1 {f1s} 오류 {[x['errors'] for x in c['scores']][:3]} {s.get('invalid', '')}")
    if kind == "출력단독":
        continue
    for rep, rr in sorted(recs.items()):
        if d.name == "s29-r10" and rep > 2:  # summary가 무효로 표시(budget 오류). 앞 3회만
            continue
        assert set(rr) == set(IDS)
        key = (d.name, rep)
        members[key] = {i: rr[i]["response"]["input"]["category"] for i in IDS}
        meta[key] = (c["config_hash"], FAMILY[d.name], c["scores"][rep]["cv"]["block_f1"], dict(src[rep]))


def f1(pred):  # pred: {id: bool block}
    tp = sum(GB[i] and pred[i] for i in IDS)
    fp = sum((not GB[i]) and pred[i] for i in IDS)
    fn = sum(GB[i] and not pred[i] for i in IDS)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


blk = {k: {i: c in B for i, c in v.items()} for k, v in members.items()}
wrong = {k: {i for i in IDS if blk[k][i] != GB[i]} for k in members}
print("\n== 검증: 다시 계산한 F1(160행 합산) 대 summary cv.block_f1, source 분포")
for k in members:
    h, fam, rec, srcs = meta[k]
    flag = "" if abs(f1(blk[k]) - rec) < 5e-4 else "  <-- 불일치"
    print(f"{k[0]:18s} r{k[1]} {h[:4]} {fam:26s} 재계산 {f1(blk[k]):.4f} 기록 {rec:.4f} 오답 {len(wrong[k]):2d} "
          f"(FP {sum(not GB[i] for i in wrong[k])} FN {sum(GB[i] for i in wrong[k])}) {srcs}{flag}")

BAD = {("s-g-medium", 0)}  # 판정 오류 74회: 기본값으로 떨어진 행이 섞여 다양성으로 세지 않는다
keys = [k for k in members if k not in BAD]


def jac(a, b):
    return len(wrong[a] & wrong[b]) / len(wrong[a] | wrong[b]) if wrong[a] | wrong[b] else 1.0


def summ(xs):
    return f"n={len(xs)} 최소 {min(xs):.2f} 중앙 {st.median(xs):.2f} 최대 {max(xs):.2f} 평균 {st.mean(xs):.2f}" if xs else "n=0"


INPUT_ONLY = [k for k in keys if "입력단독" in meta[k][1] or k[0].startswith("s-")]
print("\n== (2a/2b) 오답 Jaccard, 이중 오답 수 (차단 결정 기준)")
same = [(a, b) for a, b in it.combinations(keys, 2) if meta[a][0] == meta[b][0]]
print("같은 설정 해시 쌍 전체:", summ([jac(a, b) for a, b in same]), "| 이중오답", summ([len(wrong[a] & wrong[b]) for a, b in same]))
for h in sorted({meta[k][0] for k in keys}):
    g = [k for k in keys if meta[k][0] == h]
    if len(g) > 1:
        ps = list(it.combinations(g, 2))
        print(f"  같은 프롬프트 {h[:4]} {meta[g[0]][1]:24s}:", summ([jac(a, b) for a, b in ps]),
              "| 이중오답 평균 %.1f, 단독 오답 평균 %.1f" % (st.mean(len(wrong[a] & wrong[b]) for a, b in ps), st.mean(len(wrong[k]) for k in g)))
# 프롬프트 계열(해시가 달라도 입력 프롬프트·예시 유무가 같으면 한 계열)
PCLASS = {"6129": "A v1+예시", "8a96": "A v1+예시", "cba7": "A v1+예시", "d63c": "A' v1+예시(sublinear)",
          "d6ab": "N v1 예시없음", "be8a": "N v1 예시없음", "c5ea": "N' baseline", "6ded": "V v2a+예시",
          "e850": "V' v2a+예시(앞)", "399e": "VN v2a 예시없음", "eeae": "ACT 요청유형"}
pc = {k: PCLASS[meta[k][0][:4]] for k in keys}
cls = sorted(set(pc.values()))
print("같은 계열·다른 해시(입력단독 대 전체 경로 등) 쌍:",
      summ([jac(a, b) for a, b in it.combinations(keys, 2) if pc[a] == pc[b] and meta[a][0] != meta[b][0]]))
print("다른 프롬프트 계열 쌍:")
for x, y in it.combinations(cls, 2):
    ps = [(a, b) for a in keys for b in keys if pc[a] == x and pc[b] == y]
    print(f"  {x:22s} x {y:22s}:", summ([jac(a, b) for a, b in ps]),
          "| 이중오답 평균 %.1f" % st.mean(len(wrong[a] & wrong[b]) for a, b in ps))


def combos(triple):
    o = {i: (GB[i] if any(blk[k][i] == GB[i] for k in triple) else not GB[i]) for i in IDS}
    cnt = {i: sum(blk[k][i] for k in triple) for i in IDS}
    n = len(triple)
    return dict(single=st.mean(f1(blk[k]) for k in triple), best=max(f1(blk[k]) for k in triple), oracle=f1(o),
                any_right=sum(o[i] == GB[i] for i in IDS) / len(IDS),
                OR=f1({i: cnt[i] >= 1 for i in IDS}), AND=f1({i: cnt[i] == n for i in IDS}),
                maj=f1({i: cnt[i] * 2 > n for i in IDS}))


def report(name, triples):
    if not triples:
        return print(f"  {name}: 조합 없음")
    rs = [combos(t) for t in triples]
    a = {k: st.mean(r[k] for r in rs) for k in rs[0]}
    print(f"  {name:44s} 조합 {len(rs):3d} | 단독평균 {a['single']:.3f} 최고 {a['best']:.3f} | oracle {a['oracle']:.3f} "
          f"(하나라도 맞힘 {a['any_right']:.3f}) | OR {a['OR']:.3f} AND {a['AND']:.3f} 2of3 {a['maj']:.3f} "
          f"| oracle-단독 {(a['oracle'] - a['single']) * 45:+.2f}점 2of3-단독 {(a['maj'] - a['single']) * 45:+.2f}점")
    return a


def of(c):
    return [k for k in keys if pc[k] == c]


def of_in(c):  # 입력 단독 실행만
    return [k for k in of(c) if not k[0].startswith("s2")]


print("\n== (2c/2d) 3개 묶음: oracle·OR·AND·2-of-3 (점 = ΔF1×45)")
print(" [대조] 같은 프롬프트 3회")
ctl = {}
for h in sorted({meta[k][0] for k in keys}):
    g = [k for k in keys if meta[k][0] == h]
    if len(g) >= 3:
        ctl[h] = report(f"같은 {h[:4]} {meta[g[0]][1]}", list(it.combinations(g, 3)))
print(" [후보 대용] 다른 프롬프트 3개 (영어판 기록이 로컬에 없어 한국어 변형으로 대신함)")
A, N = of_in("A v1+예시"), of_in("N v1 예시없음")
res = {}
for third in ("V v2a+예시", "VN v2a 예시없음", "ACT 요청유형", "V' v2a+예시(앞)"):
    res[third] = report(f"입력단독: v1+예시 x v1 예시없음 x {third}", list(it.product(A, N, of(third))))
res["full"] = report("전체경로: default x combo(예시없음) x baseline",
                     list(it.product([k for k in of("A v1+예시") if k[0] in ("s29-default", "s28-smoke")],
                                     [k for k in of("N v1 예시없음") if k[0] == "s29-combo"], of("N' baseline"))))
print(" [2개 묶음] 예시 유무만 다른 쌍 대 같은 프롬프트 쌍")
report("입력단독: v1+예시 x v1 예시없음 (2개)", list(it.product(A, N)))
report("대조: v1+예시 2회 (6129)", list(it.combinations(A, 2)))
report("대조: v1 예시없음 2회 (d6ab)", list(it.combinations(N, 2)))

print("\n== (3) 행별 오답 횟수 (유효 멤버 전체, s-g 제외)")
wc = Counter(i for k in keys for i in wrong[k])
s29 = [k for k in keys if k[0].startswith("s29")]
allw = [i for i in IDS if all(i in wrong[k] for k in s29)]
print(f"멤버 {len(keys)}개 중 s29 멤버 {len(s29)}개. s29 전부에서 틀린 행 {len(allw)}개, 한 번이라도 틀린 행 {len(wc)}개")
print("차단 범주:", sorted(B))
print("오답 횟수 분포(행 수):", sorted(Counter(wc.values()).items()))
FIXED = ("2b24", "4eb4", "4bdb", "6728", "d748", "64b2", "0d37", "01d2", "b3b7")
show = sorted(set(allw) | {i for i in IDS if i[4:].startswith(FIXED)} | {i for i, c in wc.items() if c >= len(keys) * 0.6},
              key=lambda i: -wc[i])
for i in show:
    per = {c: f"{sum(i in wrong[k] for k in of(c))}/{len(of(c))}" for c in cls}
    preds = Counter(members[k][i] for k in keys if i in wrong[k])
    print(f"{i[:12]} 정답 {gold[i]:20s} 오답 {wc[i]:2d}/{len(keys)} s29 {sum(i in wrong[k] for k in s29)}/{len(s29)} "
          f"낸 범주 {dict(preds)} | 계열별 {per}")
    if "--texts" in sys.argv:
        print("     >", text[i][:400].replace("\n", " / "))
