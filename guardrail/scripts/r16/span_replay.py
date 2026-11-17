"""S3.1 Phase 1a 재생 채점기: 저장된 응답에 출력 조립 변형을 걸어 행 단위로 다시 채점한다(API 0회).

실행(guardrail/에서):
`uv run python -X utf8 scripts/r16/span_replay.py runs/<A>/responses.jsonl [runs/<B>/... ...]`
`uv run python -X utf8 scripts/r16/span_replay.py --selftest`
6.0.6(rule_span: clause) 전 기록은 `--set output.rule_span=pm4`를 붙여야 "재생 = 기록"이 맞는다.

- 출력 단 준비(답변 자르기·KB 순위·규칙 ②·문맥)는 `Guard._prepare`를 그대로 부른다
  (llm=None, 호출 없음).
- 범주는 기록된 응답의 것, nano 판정은 기록된 `output_judge` 호출의 `parsed`를 쓴다.
  채점은 실제 grader.
- 첫 검사: 변형 없는 재생(base)이 기록된 (grounded, unsupported)와 모든 행에서 같아야 한다.
  다른 행이 있는 실행은 변형을 채점하지 않는다.
- 홀드아웃 묶음 행은 뺀다. 개발 행 중 후보 답변이 있는 행만 본다.
- 변형 목록은 결과를 보기 전에 고정했다(플랜 s3.1-s3.8 §Phase 1a).
  바꾸면 결과 문서에 시도 수를 적는다.
"""

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from guardrail import rules
from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.decide import output_verdict, trusted
from guardrail.eval import cv
from guardrail.eval.run import parse_sets
from guardrail.eval.score import scoring
from guardrail.guard import Guard, build_indexes
from guardrail.judges.output_judge import quote_ok
from guardrail.schema import GuardRequest

N = scoring.normalize
WORSE = 0.2  # 채택 기준: (grounded 맞음 + 구간 F1)이 이만큼 넘게 떨어진 행은 2개까지
MAX_WORSE = 2
SHORT = 8  # 1c 기준 2: 이 글자 수(정규화 뒤) 미만 인용이 1.1보다 늘면 안 된다


def out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


# ---- 문장·절·어절 (.claude/plans/v2-research/scripts/q3_sim.py에서 옮김) ----
CONN = re.compile(r"(?:고|며|면|지만|는데|은데|서|니까|므로)$")


def sentences(a: str) -> list[tuple[int, int]]:
    """. ! ? 뒤에 공백·끝·인용 괄호가 오거나 줄바꿈이면 문장 끝. 1.3 같은 소수는 안 자른다."""
    cuts, i = [], 0
    for m in re.finditer(r"[.!?]+(?=\s|$|\[|\()|\n", a):
        cuts.append((i, m.end()))
        i = m.end()
    if i < len(a):
        cuts.append((i, len(a)))
    res = []
    for s, e in cuts:
        while s < e and a[s].isspace():
            s += 1
        if s < e:
            res.append((s, e))
    return res


def sent_of(a: str, i: int, j: int) -> tuple[int, int]:
    ss = [(s, e) for s, e in sentences(a) if s < j and i < e]
    return (ss[0][0], ss[-1][1]) if ss else (i, j)


def clause_of(a: str, i: int, j: int) -> tuple[int, int]:
    """[i, j)를 덮는 절: 문장 안에서 쉼표 뒤·연결 어미 어절 뒤에서 자른다."""
    s, e = sent_of(a, i, j)
    cuts = [s]
    for m in re.finditer(r"\S+", a[s:e]):
        end = s + m.end()
        if end >= e:
            break
        w = m.group()
        core = w.rstrip(",;:")
        if w != core or CONN.search(core):
            cuts.append(end)
    cuts.append(e)
    cs = [
        (x, y) for x, y in zip(cuts, cuts[1:], strict=False) if x < j and i < y and a[x:y].strip()
    ]
    x, y = (cs[0][0], cs[-1][1]) if cs else (s, e)
    while x < y and a[x].isspace():
        x += 1
    return x, y


def eoj_expand(a: str, i: int, j: int) -> tuple[int, int]:
    while i > 0 and not a[i - 1].isspace():
        i -= 1
    while j < len(a) and not a[j].isspace():
        j += 1
    return i, j


def eoj_back(a: str, i: int, k: int) -> int:
    """어절 시작 i에서 k어절 앞 어절의 시작."""
    for _ in range(k):
        j = i
        while j > 0 and a[j - 1].isspace():
            j -= 1
        if j == 0:
            break
        while j > 0 and not a[j - 1].isspace():
            j -= 1
        i = j
    return i


def cut(a: str, wins: list[tuple[int, int]]) -> tuple[str, ...]:
    """창을 합치고(겹치거나 맞닿으면) 원문 조각으로. 정규화 뒤 빈 조각은 버린다."""
    merged: list[tuple[int, int]] = []
    for s, e in sorted((max(0, s), min(len(a), e)) for s, e in wins):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return tuple(dict.fromkeys(a[s:e] for s, e in merged if N(a[s:e])))


# ---------------- 변형 R2: 인용 왼쪽을 KB 1위 섹션과 대조해 깎기 ----------------
def uncovered(a: str, ref_n: str, lmin: int) -> set[int]:
    """답변 원문 위치 중 정규화한 1위 섹션과 lmin자 이상 공통 부분 문자열에 들지 않는 글자.

    섹션 ID 인용은 뺀다.
    """
    chars, pos = [], []
    for idx, ch in enumerate(a):
        for c in N(ch):
            chars.append(c)
            pos.append(idx)
    s = "".join(chars)
    cov = [False] * len(s)
    for i in range(len(s)):
        length = 0
        while i + length < len(s) and s[i : i + length + 1] in ref_n:
            length += 1
        if length >= lmin:
            for x in range(i, i + length):
                cov[x] = True
    cite = {x for m in rules.CITE_RX.finditer(a) for x in range(m.start(), m.end())}
    return {pos[x] for x in range(len(s)) if not cov[x] and pos[x] not in cite}


def trim_left(a: str, quote: str, ref: str, lmin: int) -> str:
    """인용을 첫 '섹션에 없는' 어절부터 인용 끝까지로 줄인다.

    오른쪽은 안 깎는다. 깎을 게 없으면 그대로.
    """
    s = a.find(quote)
    if s < 0:
        return quote
    e = s + len(quote)
    xs = sorted(x for x in uncovered(a, N(ref), lmin) if s <= x < e)
    if not xs:
        return quote
    i = max(eoj_expand(a, xs[0], xs[0] + 1)[0], s)
    return a[i:e] if N(a[i:e]) else quote


# ---------------- 변형 R3: 규칙 ② 창 = 토큰 한 어절 앞 → 절 끝 ----------------
def r3_spans(a: str, toks: list[rules.LiteralToken]) -> tuple[str, ...]:
    first: dict[str, int] = {}  # rules.rule_spans와 같이 서로 다른 토큰 문자열마다 첫 토큰
    for t in toks:
        first.setdefault(a[t.start : t.end], t.start)
    wins = []
    for text, i in first.items():
        j = i + len(text)
        cs, ce = clause_of(a, i, j)
        wins.append((max(cs, eoj_back(a, eoj_expand(a, i, j)[0], 1)), ce))
    return cut(a, wins)


# ---------------- 변형 정의 (결과 보기 전에 고정) ----------------
@dataclass(frozen=True)
class Variant:
    name: str
    trust: str | None = None  # None = 설정값
    min_chars: int | None = None
    failure_default: str | None = None
    trim_l: int | None = None  # R2: lmin
    rule_window: str = "pm4"  # R3: "eoj1_clause"
    rule_quote: bool = False  # R1
    quote_narrow: str | None = None  # S3.12: ③ 인용을 근거 없는 토큰 둘레로 줄인다


# S3.12에서 더한 변형(결과 보기 전에 고정). 참조 = 1위 섹션(top) | nano에 준 문맥 전체(ctx),
# 창 = 한 어절 앞 → 절 끝(clause) | 토큰이 든 어절(eoj). 인용 안에 걸린 토큰이 없으면 인용 그대로
S312 = (
    Variant("Q1 인용→토큰 절(top)", quote_narrow="top_clause"),
    Variant("Q2 인용→토큰 절(ctx)", quote_narrow="ctx_clause"),
    Variant("Q3 인용→토큰 어절(ctx)", quote_narrow="ctx_eoj"),
    Variant("Q4 quote_min_chars=4", min_chars=4),
)
VARIANTS = (
    Variant("base"),
    Variant("R2 trim L, lmin=2", trim_l=2),
    Variant("R2 trim L, lmin=3", trim_l=3),
    Variant("R3 rule window 1어절 앞→절 끝", rule_window="eoj1_clause"),
    Variant("R4 tier3_trust=all", trust="all"),
    Variant("R4 tier3_trust=both_quotes", trust="both_quotes"),
    Variant("R4 quote_min_chars=1", min_chars=1),
    Variant("R4 failure_default=ungrounded", failure_default="ungrounded"),
    Variant("R1 rule 행에 인용(1.2 인용에서만 판단)", rule_quote=True),
    *S312,
)


def narrow_quote(a: str, quote: str, ref: str, extra, window: str) -> tuple[str, ...]:
    """인용 안의 '참조에 없는' 숫자·리터럴 토큰 둘레만 남긴다. 그런 토큰이 없으면 인용 그대로."""
    s = a.find(quote)
    if s < 0:
        return (quote,)
    e = s + len(quote)
    toks = [t for t in rules.unsupported_literals(a, ref, extra) if s <= t.start and t.end <= e]
    wins = []
    for t in toks:
        ws, we = eoj_expand(a, t.start, t.end)
        if window == "clause":
            cs, ce = clause_of(a, t.start, t.end)
            ws, we = max(cs, eoj_back(a, ws, 1)), ce
        wins.append((max(s, ws), min(e, we)))
    return cut(a, wins) or (quote,)


@dataclass(frozen=True)
class Facts:
    answer: str
    ctx: str  # nano에 준 문맥(kb_quote 확인용)
    ref: str  # 1위 섹션 전체(R2·규칙 토큰용)
    rule: tuple[str, ...] | None  # 기록 설정의 규칙 ② 구간
    toks: tuple[rules.LiteralToken, ...]  # 규칙 ②가 걸렸을 때 근거 없는 토큰
    output_error: bool


def facts_of(guard: Guard, row, cfg) -> Facts:
    p = guard._prepare(GuardRequest(row.messages, row.candidate_answer, True))
    if getattr(p, "output_error", None) is not None or p.answer is None:
        return Facts(p.answer or "", "", "", None, (), True)
    ref = guard.kb.section_text(p.hits[0].section_id) if p.hits else ""
    toks = ()
    if p.rule_spans is not None:
        toks = tuple(rules.unsupported_literals(p.answer, ref, cfg.output.rule_extra_tokens))
    ctx = "\n".join(h.text for h in p.hits)
    return Facts(p.answer, ctx, ref, p.rule_spans, toks, False)


def replay(
    v: Variant, f: Facts, category: str, nano: dict | None, cfg, block
) -> tuple[bool, tuple]:
    """(grounded, unsupported). guard.Guard._output과 같은 순서.

    nano는 기록된 OutputOut dict 또는 None.
    """
    o = cfg.output
    trust_mode = v.trust or o.tier3_trust
    min_chars = o.quote_min_chars if v.min_chars is None else v.min_chars
    fdefault = v.failure_default or o.failure_default
    if f.output_error:
        g, spans, _ = output_verdict(category, block, None, None, False, cfg.toggles, fdefault)
        return g, spans
    ungrounded = quote = None
    a_ok = k_ok = False
    if nano is not None:
        ungrounded = not nano["grounded"]
        a_ok = quote_ok(nano["answer_quote"], f.answer, min_chars)
        k_ok = quote_ok(nano["kb_quote"], f.ctx, 1)
        q = html.unescape(nano["answer_quote"])
        if a_ok and q in f.answer:
            quote = trim_left(f.answer, q, f.ref, v.trim_l) if v.trim_l else q
    rule = f.rule
    if rule is not None and v.rule_window == "eoj1_clause" and f.toks:
        rule = r3_spans(f.answer, list(f.toks))
    if rule is not None and v.rule_quote and ungrounded and quote:
        s = f.answer.find(quote)
        if any(s < t.end and t.start < s + len(quote) for t in f.toks):
            rule = (quote,)
    is_trusted = ungrounded is not None and trusted(trust_mode, a_ok, k_ok)
    g, spans, _ = output_verdict(
        category, block, rule, ungrounded, is_trusted, cfg.toggles, fdefault, quote
    )
    if v.quote_narrow and quote and spans == (quote,):  # ③(인용을 구간으로 쓴 행)만
        src, window = v.quote_narrow.split("_")
        ref = f.ref if src == "top" else f.ctx
        spans = narrow_quote(f.answer, quote, ref, o.rule_extra_tokens, window)
    if o.span_tidy:  # guard.Guard._output과 같이 마지막에 다듬는다(S3.10)
        spans = tuple(dict.fromkeys(rules.tidy_span(x) for x in spans))
    return g, tuple(s for s in spans if s)


# ---------------- 채점 ----------------
def total(pts: dict[str, tuple[float, float]], safe: dict[str, float]) -> float:
    """출력 평균 = (grounded + 구간 F1 + safe) / 3의 행 평균."""
    return sum((g + s + safe[i]) / 3 for i, (g, s) in pts.items()) / len(pts)


def row_points(pred: tuple[bool, tuple], lab: dict) -> tuple[float, float]:
    """(grounded 맞음, 구간 F1). safe는 변형과 무관해 기록값을 쓴다."""
    g, spans = pred
    return float(g == lab["grounded"]), scoring.span_f1(list(spans), lab["unsupported"])


def load_runs(paths: list[str]) -> dict[tuple[str, int], dict[str, dict]]:
    runs: dict[tuple[str, int], dict[str, dict]] = {}
    for p in paths:
        name = Path(p).parent.name
        for line in Path(p).read_text("utf-8").splitlines():
            r = json.loads(line)
            runs.setdefault((f"{name}:{r.get('config', '')}", r["repeat"]), {})[r["id"]] = r
    return runs


def nano_of(rec: dict) -> tuple[bool, dict | None]:
    """(출력 nano를 불렀나, parsed). 실패한 호출은 (True, None)."""
    calls = [c for c in rec.get("llm") or [] if c["role"] == "output_judge"]
    if not calls:
        return False, None
    return True, calls[-1]["parsed"]


def score_variants(rows, cat_of, recs, ctx, safe, show_rows: bool) -> None:
    """변형마다 출력 평균과 base 대비 행 단위 변화. ctx = (facts, pub, gold, cfg, block)."""
    facts, pub, gold, cfg, block = ctx
    base_pts = {}
    span_rows = [i for i in rows if i in gold]
    out(f"   {'변형':38s} 출력평균  Δ      구간F1(정답행) 바뀜 좋아짐 나빠짐 >{WORSE}  기준")
    for v in VARIANTS:
        pts = {}
        for i in rows:
            pred = replay(v, facts[i], cat_of(i), nano_of(recs[i])[1], cfg, block)
            pts[i] = row_points(pred, pub[i].labels)
        base_pts = base_pts or pts  # VARIANTS[0]이 base
        d = {i: sum(pts[i]) - sum(base_pts[i]) for i in rows}
        changed = [i for i in rows if abs(d[i]) > 1e-9]
        better = sum(d[i] > 0 for i in changed)
        worse = sum(d[i] < 0 for i in changed)
        big = sum(d[i] < -WORSE for i in changed)
        ok = changed and better * 2 > len(changed) and big <= MAX_WORSE
        span = sum(pts[i][1] for i in span_rows) / len(span_rows) if span_rows else float("nan")
        mark = "-" if v.name == "base" or not changed else ("통과" if ok else "미달")
        now, before = total(pts, safe), total(base_pts, safe)
        out(
            f"   {v.name:38s} {now:.4f} {now - before:+.4f}"
            f"  {span:.3f}          {len(changed):3d} {better:5d} {worse:5d} {big:4d}  {mark}"
        )
        if show_rows and changed and v.name != "base":
            for i in changed:
                out(f"      {i} Δ{d[i]:+.3f} (g+F1 {sum(base_pts[i]):.2f}→{sum(pts[i]):.2f})")


def quotes(recs, rows, facts, pub, block) -> dict[str, tuple[bool, bool, float]]:
    """block 아닌 정답 범주 행: (근거 없음 + 글자 그대로 인용, 8자 미만, 인용 = 구간일 때 F1)."""
    res = {}
    for i in rows:
        if pub[i].labels["category"] in block:
            continue
        p = nano_of(recs[i])[1]
        q = html.unescape(p["answer_quote"]) if p and not p["grounded"] else ""
        ok = bool(q) and quote_ok(q, facts[i].answer, 1) and q in facts[i].answer
        f1 = scoring.span_f1([q] if ok else [], pub[i].labels["unsupported"])
        res[i] = (ok, ok and len(N(q)) < SHORT, f1)
    return res


def screen(base_runs, cand_runs, ctx) -> None:
    """1c 선별 기준(플랜 §Phase 1c): 후보 실행을 기준 실행마다 짝지어 본다."""
    facts, pub, gold, _cfg, block = ctx
    for ck, crec in sorted(cand_runs.items()):
        for bk, brec in sorted(base_runs.items()):
            rows = [i for i in sorted(facts) if i in crec and i in brec]
            c, b = quotes(crec, rows, facts, pub, block), quotes(brec, rows, facts, pub, block)
            said = [i for i in rows if (p := nano_of(crec[i])[1]) and not p["grounded"]]
            said = [i for i in said if i in c]
            verbatim = sum(c[i][0] for i in said) / len(said) if said else float("nan")
            short_c, short_b = sum(v[1] for v in c.values()), sum(v[1] for v in b.values())
            pair = [i for i in c if i in gold and (c[i][0] or b[i][0])]
            d = [c[i][2] - b[i][2] for i in pair]
            dmean = sum(d) / len(d) if d else float("nan")
            big = sum(x < -WORSE for x in d)
            crit = (verbatim >= 0.95, short_c <= short_b, dmean >= 0.05, big <= MAX_WORSE)
            for i in pair:  # 나빠진 행: 인용이 없어졌나(판정 흔들림), 있는데 짧아졌나(모양)
                if c[i][2] - b[i][2] < -WORSE:
                    out(
                        f"      {i} F1 {b[i][2]:.2f}→{c[i][2]:.2f} 인용 {b[i][0]:d}→{c[i][0]:d}"
                        f" 8자미만 {b[i][1]:d}→{c[i][1]:d}"
                    )
            out(
                f"   {ck[0]} 대 {bk[0]}: 글자 그대로 {verbatim:.2f}({len(said)}개) · "
                f"{SHORT}자 미만 {short_c} 대 {short_b} · 짝 {len(pair)}행 ΔF1 {dmean:+.3f} · "
                f">{WORSE} 나빠짐 {big} → 기준 {''.join('O' if x else 'X' for x in crit)}"
            )


def main(
    paths: list[str], show_rows: bool, sets: list[str], candidate: str | None, alts=()
) -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "configs" / "default.yaml", parse_sets(sets))
    block = frozenset(cfg.block_categories)
    guard = Guard(cfg, None, *build_indexes(cfg))  # llm=None: 호출하지 않는다
    split = cv.load_splits()
    held = set(split.holdout)
    pub = {r.id: r for r in load_public(cfg.paths.public) if r.endpoint == "guard"}
    dev = sorted(
        i for i, r in pub.items() if r.candidate_answer is not None and split.cluster[i] not in held
    )
    facts = {i: facts_of(guard, pub[i], cfg) for i in dev}
    gold = {i for i in dev if pub[i].labels["unsupported"]}
    ctx = (facts, pub, gold, cfg, block)
    out(
        f"설정 {cfg.version}, 개발 후보 답변 행 {len(dev)}개(정답 구간 {len(gold)}행), "
        f"변형 {len(VARIANTS) - 1}개"
    )

    alt_ctx = {}
    for a in alts:  # "k=v;k=v" 한 묶음이 설정 변형 하나
        acfg = load_config(root / "configs" / "default.yaml", parse_sets([*sets, *a.split(";")]))
        ag = Guard(acfg, None, *build_indexes(acfg))
        alt_ctx[a] = (acfg, {i: facts_of(ag, pub[i], acfg) for i in dev})
    runs = load_runs(paths)
    for key, recs in sorted(runs.items()):
        rows = [i for i in dev if i in recs and "output" in recs[i]["response"]]
        if not any(nano_of(recs[i])[0] for i in rows):
            out(f"\n== {key}: 출력 nano 기록 없음(입력만 실행?) — 건너뜀")
            continue
        out(f"\n== {key}: 행 {len(rows)}")
        bad = []
        for i in rows:
            r = recs[i]
            pred = replay(VARIANTS[0], facts[i], r["response"]["input"]["category"],
                          nano_of(r)[1], cfg, block)  # fmt: skip
            o = r["response"]["output"]
            if pred != (o["grounded"], tuple(o["unsupported"])):
                bad.append(i)
        if bad:
            out(f"   재생 ≠ 기록 {len(bad)}행: {bad[:10]} — 이 실행은 변형을 채점하지 않는다")
            continue
        out("   재생 = 기록: 모든 행 OK")
        safe = {
            i: float(recs[i]["response"]["output"]["safe"] == pub[i].labels["safe"]) for i in rows
        }
        cat_of = lambda i, r=recs: r[i]["response"]["input"]["category"]  # noqa: E731
        score_variants(rows, cat_of, recs, ctx, safe, show_rows)
        for name, (acfg, afacts) in alt_ctx.items():
            base = {i: row_points(replay(VARIANTS[0], facts[i], cat_of(i), nano_of(recs[i])[1],
                                         cfg, block), pub[i].labels) for i in rows}  # fmt: skip
            pts = {i: row_points(replay(VARIANTS[0], afacts[i], cat_of(i), nano_of(recs[i])[1],
                                        acfg, block), pub[i].labels) for i in rows}  # fmt: skip
            d = {i: sum(pts[i]) - sum(base[i]) for i in rows}
            ch = [i for i in rows if abs(d[i]) > 1e-9]
            now, up = total(pts, safe), sum(d[i] > 0 for i in ch)
            out(
                f"   ALT {name:44s} {now:.4f} {now - total(base, safe):+.4f}"
                f"  바뀜 {len(ch)} 좋아짐 {up} 나빠짐 {len(ch) - up}"
                f" >{WORSE} {sum(d[i] < -WORSE for i in ch)}"
            )
            if show_rows:
                for i in ch:
                    out(f"      {i} Δ{d[i]:+.3f} (g+F1 {sum(base[i]):.2f}→{sum(pts[i]):.2f})")

    if candidate:  # 출력 단독 기록: 범주를 normal로 두고 구간을 안 내서 재생 검사는 못 한다
        cand = load_runs([candidate])
        out("\n== 선별(정답 범주로 조립, safe는 맞음으로 둠 — Δ만 볼 것)")
        for key, recs in sorted(cand.items()):
            rows = [i for i in dev if i in recs and nano_of(recs[i])[0]]
            out(f"\n== {key}: 행 {len(rows)}")
            score_variants(rows, lambda i: pub[i].labels["category"], recs, ctx,
                           dict.fromkeys(rows, 1.0), show_rows)  # fmt: skip
        out(
            "\n   기준: 글자 그대로 ≥0.95 · 8자 미만 안 늘어남 · 짝 ΔF1 ≥ +0.05 · "
            "0.2 넘게 나빠진 행 ≤2"
        )
        screen(runs, cand, ctx)


# ---------------- 자체 검사(데이터·API 없이) ----------------
def check(ok: bool, msg: object = "") -> None:
    if not ok:  # python -O에서도 남게 assert 대신
        raise SystemExit(f"selftest 실패: {msg!r}")


def selftest() -> None:
    a = "수강료는 월 5만 원입니다. 환불은 수강 시작 후 7일 안에만 가능하고, 이후에는 안 됩니다."
    ref = "수강료는 월 3만 원입니다. 환불은 수강 시작 후 14일 안에 가능합니다."
    q = "환불은 수강 시작 후 7일 안에만 가능하고"
    t = trim_left(a, q, ref, 3)
    check(a.find(t) >= 0 and q.endswith(t), t)  # 원문 그대로, 오른쪽은 그대로
    check(t.startswith("7일"), t)  # 섹션에 있는 도입("환불은 수강 시작 후")은 깎인다
    check(trim_left(a, "수강료는 월", ref, 3) == "수강료는 월")  # 깎을 게 없으면 그대로
    toks = rules.extract_literals(a)
    five = [x for x in toks if a[x.start : x.end].startswith("5")]
    check(r3_spans(a, five) == ("월 5만 원입니다.",), r3_spans(a, five))  # 한 어절 앞 → 절 끝
    b = "기간은 연장되지만 수강료 5만 원은 그대로이고, 환불은 안 됩니다."
    check(r3_spans(b, rules.extract_literals(b)) == ("수강료 5만 원은 그대로이고,",))
    check(cut(a, [(0, 3), (2, 6), (40, 40)]) == (a[0:6],))
    check(sent_of(a, 20, 22)[0] == a.find("환불"))
    out("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("responses", nargs="*")
    ap.add_argument("--rows", action="store_true", help="바뀐 행의 id와 점수 변화를 찍는다")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument(
        "--candidate", help="1c 선별: 출력 단독(--output-only) 기록. 위치 인자는 1.1 기준 기록"
    )
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        dest="sets",
        help="기록 당시 설정으로(예: output.rule_span=pm4)",
    )
    ap.add_argument(
        "--alt", action="append", default=[], dest="alts",
        help="설정 변형 하나(예: output.rule_min_margin=3.0). 여러 키는 ;로 잇는다",
    )  # fmt: skip
    args = ap.parse_args()
    selftest()
    if not args.selftest:
        if not args.responses:
            ap.error("responses.jsonl 경로가 필요하다")
        main(args.responses, args.rows, args.sets, args.candidate, args.alts)
