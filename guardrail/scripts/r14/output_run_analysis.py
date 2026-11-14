"""출력 단독 실측(`--output-only`)의 응답을 분석한다(API 0회).

실행(guardrail/에서):
`uv run python -X utf8 scripts/r14/output_run_analysis.py runs/<name>/responses.jsonl`
볼 것: ① nano "근거 없음"의 정밀도·재현율(non-block 행, 규칙 ②가 안 걸린 행 따로)
② 인용 확인 범위별(all / answer_quote / both_quotes) 정밀도·건수와 인용이 맞는 비율
③ 확인된 answer_quote를 구간으로 냈을 때의 구간 F1 ④ refuses와 safe 라벨 ⑤ 실패 행과 뒤집힘
⑥ 조립 방식별 출력 평균(범주는 정답을 쓴다).
문맥·질의는 하네스의 OutputOnlyGuard와 같은 방식으로 다시 만든다.
"""

import html
import json
import sys
from collections import Counter
from pathlib import Path

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval.score import scoring
from guardrail.judges.output_judge import output_context, quote_ok
from guardrail.kb import KbIndex
from guardrail.rules import literal_rule

root = Path(__file__).resolve().parents[2]
cfg = load_config(root / "configs" / "default.yaml")
BLOCK = frozenset(cfg.block_categories)
kb = KbIndex.load(cfg.paths.kb, ngram=cfg.kb.ngram, k1=cfg.kb.k1, b=cfg.kb.b)
pub = {r.id: r for r in load_public(cfg.paths.public)}
recs = [json.loads(x) for x in Path(sys.argv[1]).read_text("utf-8").splitlines()]


def out(line=""):
    sys.stdout.write(line + "\n")


def facts(rid):
    """행의 답변·문맥·규칙 ② 결과(실행과 무관, 한 번만 계산)."""
    row = pub[rid]
    answer = row.candidate_answer[: cfg.input.max_answer_chars]
    users = [m.content[: cfg.input.max_chars] for m in row.messages if m.role == "user"]
    half = cfg.retrieval.max_query_chars // 2
    ranking = kb.rank("\n".join(users)[-half:] + "\n" + answer[:half])
    ctx = output_context(ranking, kb, cfg)
    rule = literal_rule(answer, list(ranking.hits[: cfg.kb.top_k]), ranking.margin, kb, cfg)
    return answer, "\n".join(h.text for h in ctx), rule


cache = {}
ids = sorted({r["id"] for r in recs if pub[r["id"]].candidate_answer})
for i in ids:
    cache[i] = facts(i)

labels = Counter()
for i in ids:
    lab = pub[i].labels
    labels[("block" if lab["category"] in BLOCK else "non-block", lab["grounded"])] += 1
out(f"후보 답변이 있는 행 {len(ids)}개: {dict(labels)}")
out(f"규칙 ②가 걸린 행: {sum(cache[i][2] is not None for i in ids)}")


def pr(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    return f"정밀도 {p:.3f} 재현율 {r:.3f} (맞음 {tp}, 오탐 {fp}, 놓침 {fn})"


for rep in sorted({r["repeat"] for r in recs}):
    cur = {r["id"]: r for r in recs if r["repeat"] == rep and r["id"] in cache}
    out(f"\n== repeat {rep}: 실패 {sum(bool(r['errors']) for r in cur.values())}/{len(cur)}")
    tally = Counter()
    spans = []
    refuse = Counter()
    quotes = Counter()
    for i, r in cur.items():
        lab = pub[i].labels
        parsed = r["llm"][0]["parsed"] if r["llm"] else None
        if parsed is None:
            continue
        answer, ctx, rule = cache[i]
        block = lab["category"] in BLOCK
        refuse[("block" if block else "non-block", f"safe={lab['safe']}", parsed["refuses"])] += 1
        if block:
            continue  # ①: block 범주는 nano 판정을 쓰지 않는다
        gold_un = not lab["grounded"]
        said_un = not parsed["grounded"]
        a_ok = quote_ok(parsed["answer_quote"], answer, cfg.output.quote_min_chars)
        a_ok0 = quote_ok(parsed["answer_quote"], answer, 1)
        k_ok = quote_ok(parsed["kb_quote"], ctx, 1)
        if said_un:
            quotes["근거 없음 판정"] += 1
            quotes["answer_quote 확인(8자)"] += a_ok
            quotes["answer_quote 확인(1자)"] += a_ok0
            quotes["kb_quote 확인"] += k_ok
            quotes["kb_quote 비움"] += not parsed["kb_quote"].strip()
        for scope in ("전체", "규칙 ② 안 걸림"):
            if scope != "전체" and rule is not None:
                continue
            for name, trust in (
                ("all", said_un),
                ("answer_quote(8자)", said_un and a_ok),
                ("answer_quote(1자)", said_un and a_ok0),
                ("both_quotes", said_un and a_ok0 and k_ok),
            ):
                key = (scope, name)
                tally[key, "tp"] += trust and gold_un
                tally[key, "fp"] += trust and not gold_un
                tally[key, "fn"] += (not trust) and gold_un
        if said_un and a_ok0 and gold_un:
            spans.append(scoring.span_f1([parsed["answer_quote"]], lab["unsupported"]))
    for scope in ("전체", "규칙 ② 안 걸림"):
        out(f"   non-block, {scope}")
        for name in ("all", "answer_quote(8자)", "answer_quote(1자)", "both_quotes"):
            k = (scope, name)
            out(f"     {name:18s} {pr(tally[k, 'tp'], tally[k, 'fp'], tally[k, 'fn'])}")
    out(f"   인용: {dict(quotes)}")
    if spans:
        mean = sum(spans) / len(spans)
        out(f"   확인된 answer_quote를 구간으로 낼 때 구간 F1 평균 {mean:.3f} (n={len(spans)})")
    out(f"   refuses (범주, safe 라벨, nano refuses): {dict(sorted(refuse.items(), key=str))}")


# ---- 실패 행, 실행 사이 뒤집힘, 조립 방식별 출력 점수(범주는 정답을 쓴다) ----
reps = sorted({r["repeat"] for r in recs})
by = {(r["repeat"], r["id"]): r for r in recs if r["id"] in cache}


def nano(rep, i):
    r = by[rep, i]
    return r["llm"][0]["parsed"] if r["llm"] and not r["errors"] else None


out("\n== 실패 행(라벨, 답변 글자 수, 문맥 글자 수, 실패한 실행)")
for i in ids:
    failed = [rep for rep in reps if nano(rep, i) is None]
    if failed:
        lab = pub[i].labels
        sizes = f"답변 {len(cache[i][0])} 문맥 {len(cache[i][1])}"
        out(f"   {i} {lab['category']} grounded={lab['grounded']} {sizes} {failed}")

nb = [i for i in ids if pub[i].labels["category"] not in BLOCK]
flips = [i for i in nb if len({(p := nano(rep, i)) and p["grounded"] for rep in reps}) > 1]
out(f"\n실행 사이에 grounded가 바뀌거나 실패가 섞인 non-block 행: {len(flips)}/{len(nb)}")
always_miss = [
    i
    for i in nb
    if not pub[i].labels["grounded"]
    and all((p := nano(rep, i)) is None or p["grounded"] for rep in reps)
]
by_rule = sum(cache[i][2] is not None for i in always_miss)
out(f"3회 모두 놓친 근거 없음 행: {len(always_miss)} (규칙 ②가 잡는 행 {by_rule})")
rule_nb = [i for i in nb if cache[i][2] is not None]
rule_right = sum(not pub[i].labels["grounded"] for i in rule_nb)
out(f"규칙 ②: non-block {len(rule_nb)}행에 걸림, 그중 정답이 근거 없음 {rule_right}")


def verdict(policy, rep, i):
    """(grounded, 구간) — non-block 행. 실패는 근거 있음(기본값)."""
    answer, _ctx, rule = cache[i]
    p = nano(rep, i)
    quote = None
    if p and not p["grounded"]:
        ok = quote_ok(p["answer_quote"], answer, 1)
        quote = [html.unescape(p["answer_quote"])] if ok else []
    if policy == "규칙 ②만":
        return (False, list(rule)) if rule is not None else (True, [])
    if policy == "② → ③(구간 없음)":
        return (False, list(rule)) if rule is not None else (quote is None, [])
    if policy == "② → ③(인용을 구간으로)":
        return (False, list(rule)) if rule is not None else (quote is None, quote or [])
    if policy == "③(인용) → ②":
        if quote:
            return False, quote
        return (False, list(rule)) if rule is not None else (quote is None, [])
    raise ValueError(policy)


out("\n== 조립 방식별 출력 평균(64행, 범주 정답 가정, block 행은 ①: true/[]/safe=false)")
for policy in ("규칙 ②만", "② → ③(구간 없음)", "② → ③(인용을 구간으로)", "③(인용) → ②"):
    line = []
    for rep in reps:
        g = s = f = 0.0
        for i in ids:
            lab = pub[i].labels
            block = lab["category"] in BLOCK
            pg, ps = (True, []) if block else verdict(policy, rep, i)
            g += pg == lab["grounded"]
            s += scoring.span_f1(ps, lab["unsupported"])
            f += (not block) == lab["safe"]
        n = len(ids)
        parts = f"g {g / n:.3f}, 구간 {s / n:.3f}, safe {f / n:.3f}"
        line.append(f"{(g + s + f) / 3 / n:.3f} ({parts})")
    out(f"   {policy:24s} " + " · ".join(line))
