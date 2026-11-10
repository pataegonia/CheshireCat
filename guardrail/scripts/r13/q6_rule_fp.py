"""Q6 (santa 4라운드): 규칙 ② 오탐 재측정 — V0/V1/V2 변형, 점수 차(margin) 게이트, 병합 span F1, 거절 정규식 뷰.

G = guard 후보 grounded=true·비block(32), T = tutor 참조 답변 action∈{answer,scaffold,abstain}·evidence 있음(119),
U = guard ungrounded(35). 실행: `python -X utf8 guardrail/scripts/r13/q6_rule_fp.py` (repo 루트).
"""

import re

from common import BLOCK_CATS, BM25, all_user, guard_rows, kb_chunks, load_kb, norm, scoring, tutor_rows
from extra_stats import REFUSAL_RX
from q5_ungrounded import ANN, NUM_RX, sections_with

CITE_RX = re.compile(r"\(?\b(?:PY|PR|FIN)-\d{2}-\d{2}\b\)?")
NUM_RX_V2 = re.compile(
    NUM_RX.pattern.replace(
        r"[한두세네다섯]\s?(?:번|개|단계|가지|배)", r"(?:한|두|세|네|다섯)\s?(?:번|개|단계|가지|배)"
    )
)
assert NUM_RX_V2.pattern != NUM_RX.pattern
assert not NUM_RX_V2.search("다 개") and NUM_RX_V2.search("다섯 개")
assert CITE_RX.sub("", "기록돼요. (PY-01-01)") == "기록돼요. "

VARIANTS = {
    "V0": lambda a: NUM_RX.findall(a),
    "V1": lambda a: NUM_RX.findall(CITE_RX.sub("", a)),
    "V2": lambda a: NUM_RX_V2.findall(CITE_RX.sub("", a)),
}


def corpora():
    g = [r for r in guard_rows() if "candidate_answer" in r]
    U = [(r["id"], all_user(r), r["candidate_answer"], r) for r in g
         if r["labels"]["category"] == "normal" and not r["labels"]["grounded"]]
    G = [(r["id"], all_user(r), r["candidate_answer"], r) for r in g
         if r["labels"]["category"] not in BLOCK_CATS and r["labels"]["grounded"]]
    t_all = [r for r in tutor_rows() if r["labels"]["action"] in ("answer", "scaffold", "abstain")]
    T = [(r["id"], all_user(r), r["labels"]["reference_answer"], r) for r in t_all if r["labels"]["evidence"]]
    assert (len(U), len(G), len(t_all)) == (35, 32, 147), (len(U), len(G), len(t_all))
    return U, G, T, t_all


class Retriever:
    def __init__(self, kb):
        self.kb, self.ch = kb, kb_chunks(kb)
        self.bm = BM25([c[2] for c in self.ch])

    def top1(self, query):
        """(top-1 섹션, margin = top-1 섹션 점수 − 다른 섹션 최고 점수). 섹션 점수 = 청크 점수 최대."""
        best = {}
        for (sid, _, _), s in zip(self.ch, self.bm.scores(query)):
            best[sid] = max(best.get(sid, 0.0), s)
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        return ranked[0][0], ranked[0][1] - ranked[1][1]


def missing(variant, ans, ref_text):
    nref = norm(ref_text)
    return [t for t in VARIANTS[variant](ans) if t.strip() and norm(t) not in nref]


def merged_pm4_spans(ans, toks, k=4):
    """토큰 첫 등장 ±k자 창, 겹치는 창 병합, 중복 제거."""
    wins = sorted({(max(0, i - k), i + len(t) + k) for t in dict.fromkeys(toks) if (i := ans.find(t)) >= 0})
    out = []
    for s, e in wins:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return list(dict.fromkeys(ans[s:e] for s, e in out))


assert merged_pm4_spans("abcdefghijklmnop", ["e", "g"], 2) == ["cdefghi"]


def rule_table(ret, U, G, T):
    print("## 6.1 규칙 ② 변형별 경보 (BM25 top-1 섹션 참조, 질의 = 전체 user + 답변)")
    print("| 변형 | U 경보(탐지) | G 오탐 | T 오탐 | 정밀도 U/(U+G+T) |")
    print("|---|---|---|---|---|")
    info = {}
    for name, rows in (("U", U), ("G", G), ("T", T)):
        for rid, q, a, _ in rows:
            info[(name, rid)] = ret.top1(q + "\n" + a)
    res = {}
    for v in VARIANTS:
        hit = {n: [rid for rid, _, a, _ in rows if missing(v, a, ret.kb[info[(n, rid)][0]])]
               for n, rows in (("U", U), ("G", G), ("T", T))}
        res[v] = hit
        tp, fp = len(hit["U"]), len(hit["G"]) + len(hit["T"])
        print(f"| {v} | {tp}/35 | {len(hit['G'])}/32 | {len(hit['T'])}/{len(T)} | {tp}/{tp + fp} = {tp / max(1, tp + fp):.2f} |")
    for v in ("V0", "V2"):
        fps = [(n, rid, missing(v, a, ret.kb[info[(n, rid)][0]]))
               for n, rows in (("G", G), ("T", T)) for rid, _, a, _ in rows if rid in res[v][n]]
        print(f"- {v} 오탐 예(상위 8): {fps[:8]}")
    gold_fp = [rid for rid, _, a, r in T if missing("V2", a, "\n".join(ret.kb[s] for s in r["labels"]["evidence"]))]
    print(f"- 참고: V2를 BM25 대신 tutor evidence 섹션과 비교하면 T 오탐 {len(gold_fp)}/{len(T)} {gold_fp}")
    return res, info


def margin_table(ret, U, G, T, res, info):
    print("\n## 6.2 V2 + 점수 차 게이트 (margin ≥ m일 때만 규칙 발화)")
    hits = [(n, info[(n, rid)][1]) for n in ("U", "G", "T") for rid in res["V2"][n]]
    ms = sorted(m for _, m in hits)
    qs = [0.0] + [ms[int(p * (len(ms) - 1))] for p in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)]
    print("| m | U 탐지 | G 오탐 | T 오탐 | 정밀도 P (U vs G∪T) |")
    print("|---|---|---|---|---|")
    for m in sorted(dict.fromkeys(round(x, 2) for x in qs + [3.5, 10.0, 30.0])):
        c = {n: sum(1 for k, x in hits if k == n and x >= m) for n in ("U", "G", "T")}
        tp, fp = c["U"], c["G"] + c["T"]
        print(f"| {m:.2f} | {tp}/35 | {c['G']}/32 | {c['T']}/{len(T)} | {tp / max(1, tp + fp):.2f} |")
    print("- 경보 행 margin(정렬):", [(n, round(x, 2)) for n, x in sorted(hits, key=lambda h: h[1])])


def span_table(ret, kb, U, res, info):
    print("\n## 6.3 V2 경보 행의 기본 span(pm4, 겹침 병합·중복 제거) F1")
    for ref_name in ("BM25 top-1", "정답 섹션"):
        f1s = []
        for rid, _, a, r in U:
            ref = kb[info[("U", rid)][0]] if ref_name == "BM25 top-1" else "\n".join(
                kb[s] for s in sections_with(kb, ANN[rid][0]))
            toks = missing("V2", a, ref)
            if toks:
                f1s.append(scoring.span_f1(merged_pm4_spans(a, toks), r["labels"]["unsupported"]))
        print(f"- 참조 {ref_name}: 경보 {len(f1s)}행, 평균 span F1 {sum(f1s) / max(1, len(f1s)):.3f}")


QUOTE_CODE_RX = re.compile(r"```.*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”|‘[^’]*’|'[^']*'|「[^」]*」|『[^』]*』", re.S)
JOIN_RX = re.compile(r"(?<!\S)[가-힣](?: [가-힣](?!\S))+")


def joined(s):
    return JOIN_RX.sub(lambda m: m.group(0).replace(" ", ""), s)


assert joined("광 고 문구") == "광고 문구" and joined("자 살펴볼까요") == "자 살펴볼까요"


def refusal_table():
    print("\n## 6.4 거절 정규식(extra_stats.REFUSAL_RX) 뷰별 분리력")
    t = tutor_rows()
    ca = [r for r in guard_rows() if "candidate_answer" in r]
    pos = [r["labels"]["reference_answer"] for r in t if r["labels"]["action"] in ("refuse", "redirect")]
    comp = [(r["id"], r["candidate_answer"]) for r in ca if r["labels"]["category"] in BLOCK_CATS]
    normal = [r["candidate_answer"] for r in ca if r["labels"]["category"] not in BLOCK_CATS]
    views = {
        "원문": lambda s: s,
        "원문 − 인용·코드": lambda s: QUOTE_CODE_RX.sub(" ", s),
        "붙인 뷰(joined)": joined,
        "붙인 뷰 − 인용·코드": lambda s: joined(QUOTE_CODE_RX.sub(" ", s)),
    }
    print("| 뷰 | tutor 거절형 감지 | block 순응 오감지 | normal 감지 |")
    print("|---|---|---|---|")
    for name, f in views.items():
        p = sum(bool(REFUSAL_RX.search(f(s))) for s in pos)
        fp = [rid for rid, s in comp if REFUSAL_RX.search(f(s))]
        nn = sum(bool(REFUSAL_RX.search(f(s))) for s in normal)
        print(f"| {name} | {p}/{len(pos)} | {len(fp)}/{len(comp)} {fp} | {nn}/{len(normal)} |")


def main():
    kb = load_kb()
    U, G, T, t_all = corpora()
    print(f"코퍼스: U {len(U)}, G {len(G)}, T {len(T)} (answer/scaffold/abstain 전체 {len(t_all)})\n")
    ret = Retriever(kb)
    res, info = rule_table(ret, U, G, T)
    margin_table(ret, U, G, T, res, info)
    span_table(ret, kb, U, res, info)
    refusal_table()


if __name__ == "__main__":
    main()
