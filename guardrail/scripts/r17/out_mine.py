"""S3.13 출력 판정 놓침 캐기: nano가 "근거 있음"이라 해서 놓친 원인과 대안을 기록만으로 본다(API 0회).

실행(guardrail/에서):
`uv run python -X utf8 scripts/r17/out_mine.py --runs runs/A runs/B [--sent-runs runs/C ...] [--section p|q|s|all]`
`uv run python -X utf8 scripts/r17/out_mine.py --selftest`

- --runs = 인용 모드(기본 출력 판정) 실행, --sent-runs = output.judge_mode: sentences 실행.
- p: 문항별 nano "근거 없음" 확률 p_i와 반복 호출(OR·AND·다수결) 모의. 호출 독립 가정, nano 판정 수준만.
- q: 정답 근거 없음 행에서 nano가 잡았을 때의 인용 품질, 잡은 건 대 놓친 건의 토큰·지연.
- s: 문장별 모드 기록으로 판정 변형 v0–v5를 다시 계산(nano 판정 수준).
- 실행 선별·정답 붙이기는 stage0.read_recs·paired_compare.make_ctx를 그대로 쓴다.
  문항·답변·자료·인용 문구는 출력하지 않는다(id 앞 8자·숫자·라벨만).
- 기록 필드 가정(원격에서 틀릴 수 있음): llm[].role == "output_judge"의 parsed = {answer_quote, kb_quote,
  grounded, refuses}(문장 모드는 checks[{n, kb_phrase, verdict}] 추가), completion_tokens, latency_ms,
  answer_quote_ok, tier. 출력 행의 정답 = labels(safe·grounded·unsupported). 정답 근거 없음 =
  채점기와 같게 safe이고 grounded가 아닌 행(output_label == ungrounded).
- shortcut: 후보 답변은 공개 세트 원문을 쓴다. Guard가 긴 답변을 자르면 문장 번호가 어긋날 수 있다.
"""

import argparse
import html
import math
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]
import stage0 as s0  # noqa: E402  (paired_compare·scoring도 함께 import됨)
from stage0 import N_OUT_PARTS, OUT_W, calls_of, dist, mean, out, parsed, pct, tier_name  # noqa: E402

import paired_compare as pc  # noqa: E402
from guardrail.eval.score import scoring  # noqa: E402
from guardrail.judges.output_judge import MAX_SENTENCES, quote_ok  # noqa: E402
from guardrail.preprocess import reveal_hidden  # noqa: E402
from guardrail.rules import sentences  # noqa: E402

N = scoring.normalize
BINS = ("0", "(0,0.2]", "(0.2,0.5]", "(0.5,0.8]", "(0.8,1)", "1")
KB_MIN_CHARS = 4  # v3의 kb_phrase 검증 최소 글자 수(요구 사양)
VARIANTS = ("v0", "v1", "v1o", "v2", "v3", "v4", "v5")
VNOTE = {
    "v0": "모델 grounded 그대로", "v1": "flagged(conflicts|not_in_material) 하나라도",
    "v1o": "v0 또는 v1(실제 코드)", "v2": "conflicts만", "v3": "conflicts + kb_phrase가 KB에 있음",
    "v4": "v3 + answer_quote 검증 통과", "v5": "not_in_material만",
}  # fmt: skip


# ---------------- 공통: 행·관측 모으기 ----------------
def gold_ungrounded(row) -> bool:
    """채점기의 output_label == ungrounded: safe이고 grounded가 아닌 행."""
    return bool(row.labels.get("safe", True)) and not row.labels["grounded"]


def nano_of(rec: dict) -> tuple[str, dict | None, dict | None]:
    """(상태, parsed, 호출). 상태 = 있음 | 없음(output_judge 호출 없음) | 파싱실패(parsed가 dict 아님)."""
    cs = calls_of(rec, "output_judge")
    if not cs:
        return "없음", None, None
    for c in reversed(cs):
        if isinstance(c.get("parsed"), dict):
            return "있음", c["parsed"], c
    return "파싱실패", None, cs[-1]


def collect(ctx: dict, runs: dict) -> dict[str, list[dict]]:
    """출력 행 id → 관측 목록. 관측 = {run, rec, state, nano, call, row}."""
    res: dict[str, list[dict]] = {}
    for run, recs in sorted(runs.items()):
        for r in ctx["rows"]:
            rec = recs.get(r.id)
            if rec is None or r.candidate_answer is None:
                continue
            st, nano, call = nano_of(rec)
            res.setdefault(r.id, []).append(
                {"run": run, "rec": rec, "state": st, "nano": nano, "call": call, "row": r}
            )
    return res


def says_ungrounded(o: dict) -> bool | None:
    """관측의 nano가 근거 없음이라 했는가. grounded가 bool이 아니면 None."""
    g = (o["nano"] or {}).get("grounded")
    return None if not isinstance(g, bool) else not g


def bin_of(p: float) -> str:
    if p <= 0:
        return BINS[0]
    if p >= 1:
        return BINS[5]
    return BINS[1] if p <= 0.2 else BINS[2] if p <= 0.5 else BINS[3] if p <= 0.8 else BINS[4]


def fmt(x: float, w: int = 6, d: int = 3) -> str:
    return f"{'-':>{w}}" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:{w}.{d}f}"


# ---------------- p. 문항별 확률과 반복 호출 모의 ----------------
def item_probs(obs: dict[str, list[dict]]) -> dict[str, dict]:
    """문항별 {gold(정답 근거 없음), n, k, p, final(최종 근거 없음 비율), tiers, block}. n = nano 판정 횟수."""
    res = {}
    for i, os_ in obs.items():
        flags = [f for o in os_ if (f := says_ungrounded(o)) is not None]
        fin = []
        for o in os_:
            pr = parsed(o["rec"], True)
            if pr is not None:
                fin.append(not pr["output"]["grounded"])
        r = os_[0]["row"]
        res[i] = {
            "gold": gold_ungrounded(r), "n": len(flags), "k": sum(flags),
            "p": sum(flags) / len(flags) if flags else float("nan"),
            "final": mean(fin) if fin else float("nan"), "nfin": len(fin),
            "tiers": Counter(tier_name(o["rec"]) for o in os_), "block": r.labels["decision"] == "block",
        }  # fmt: skip
    return res


def detect(scheme: str, p: float) -> float:
    """호출 독립 가정에서 방식이 "근거 없음"이라 할 확률(p = 1회 호출이 그렇게 말할 확률)."""
    if scheme in ("b", "e"):
        return 1 - (1 - p) ** 2  # 2회 중 하나라도
    if scheme == "c":
        return p * p
    if scheme == "d":
        return 3 * p * p * (1 - p) + p**3
    return p


def calls_of_scheme(scheme: str, p: float) -> float:
    return {"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0}.get(scheme, 1 + (1 - p))


SCHEMES = {
    "a": "(a) 1회(현재)", "b": "(b) 2회 중 하나라도 없음(OR)", "c": "(c) 2회 모두 없음(AND)",
    "d": "(d) 3회 다수결", "e": "(e) OR-2, 1회차가 근거 있음일 때만 2회차",
}  # fmt: skip


def simulate(rows: list[tuple[bool, float]]) -> dict[str, dict[str, float]]:
    """rows = [(정답 근거 없음, p_i)] → 방식별 {miss: 놓침 수, fa: 오경보 수, acc: 정확도, calls: 호출 수}(행 한 번씩)."""
    res = {}
    for s in SCHEMES:
        miss = sum(1 - detect(s, p) for g, p in rows if g)
        fa = sum(detect(s, p) for g, p in rows if not g)
        res[s] = {
            "miss": miss, "fa": fa, "acc": 1 - (miss + fa) / len(rows) if rows else float("nan"),
            "calls": sum(calls_of_scheme(s, p) for _, p in rows),
        }  # fmt: skip
    return res


def sec_p(ctx: dict, runs: dict, top: int) -> None:
    out("\n== p. 문항별 nano 판정 확률과 반복 호출 모의(인용 모드 --runs) ==")
    obs = collect(ctx, runs)
    n_out = sum(r.candidate_answer is not None for r in ctx["rows"])
    states = Counter(o["state"] for os_ in obs.values() for o in os_)
    out(f"출력 행 {n_out}개(기록에 있는 {len(obs)}개), 관측 {sum(states.values())}건, nano 상태 {dist(states)}")
    ip = item_probs(obs)
    use = {i: v for i, v in ip.items() if v["n"] > 0}
    out(f"nano 판정이 있는 행 {len(use)}개(정답 근거 없음 {sum(v['gold'] for v in use.values())}개)")
    if not use:
        out("nano 판정(llm 기록의 output_judge parsed.grounded)이 없어 이 절을 건너뛴다")
        return
    out("\n(1) 정답 근거 없음 행, p_i(nano가 근거 없음이라 한 비율) 오름차순")
    out("id       n   p_i  최종비율(n)  tier 분포")
    for i, v in sorted(((i, v) for i, v in use.items() if v["gold"]), key=lambda x: (x[1]["p"], x[0]))[:top]:
        out(f"{i[:8]:<9}{v['n']:>2}{fmt(v['p'], 6, 2)}{fmt(v['final'], 8, 2)}({v['nfin']:>2})  {dist(v['tiers'])}")
    out("\n(2) 정답 근거 있음 행 중 p_i > 0(오경보 후보), p_i 내림차순")
    out("id       n   p_i  최종비율(n)  입력 정답=차단  tier 분포")
    fa_rows = [(i, v) for i, v in use.items() if not v["gold"] and v["p"] > 0]
    for i, v in sorted(fa_rows, key=lambda x: (-x[1]["p"], x[0]))[:top]:
        out(f"{i[:8]:<9}{v['n']:>2}{fmt(v['p'], 6, 2)}{fmt(v['final'], 8, 2)}({v['nfin']:>2})  {'차단' if v['block'] else '허용':<12}  {dist(v['tiers'])}")
    out(f"  (p_i > 0인 정답 근거 있음 행 {len(fa_rows)}개 중 표는 상위 {min(top, len(fa_rows))}개)")
    out("\np_i 히스토그램(행 수)")
    out(f"{'정답':<10}" + "".join(f"{b:>11}" for b in BINS))
    for name, g in (("근거 없음", True), ("근거 있음", False)):
        c = Counter(bin_of(v["p"]) for v in use.values() if v["gold"] == g)
        out(f"{name:<10}" + "".join(f"{c[b]:>11}" for b in BINS))
    sim = simulate([(v["gold"], v["p"]) for v in use.values()])
    base = sim["a"]
    out(f"\n모의(행 {len(use)}개, 회당 = 행마다 한 번씩 판정). 점수 환산 = 100 × {OUT_W:.2f} × Δ건수 ÷ 출력 {n_out}행 ÷ {N_OUT_PARTS}(근거 성분만)")
    out(f"{'방식':<34}{'놓침/회':>8}{'오경보/회':>10}{'정확도×100':>11}{'호출/회':>9}{'추가호출':>9}{'가드점수Δ':>10}")
    for s, name in SCHEMES.items():
        v = sim[s]
        d = (base["miss"] + base["fa"]) - (v["miss"] + v["fa"])  # 줄어든 오답 건수
        out(
            f"{name:<34}{v['miss']:8.2f}{v['fa']:10.2f}{100 * v['acc']:11.2f}{v['calls']:9.1f}"
            f"{v['calls'] - base['calls']:9.1f}{100 * OUT_W * d / n_out / N_OUT_PARTS:+10.3f}"
        )
    out("가정·한계: (1) 같은 문항의 호출이 서로 독립(실제로는 같은 프롬프트라 상관이 크면 OR·AND의 이득이 과대)")
    out("  (2) p_i는 행마다 n회(실행 수) 표본 추정이라 n이 작으면 잡음이 크다 (3) nano 판정 수준만: 규칙 ② 단·인용 검증")
    out("  ·구간 점수(span, safe)는 무시, ①단 결과도 무시 (4) 가드 점수 Δ는 오답 건수 변화를 근거 성분에만 환산한 값")


# ---------------- q. 잡았을 때의 인용 품질 ----------------
def quote_metrics(o: dict) -> dict:
    """잡은 관측 하나의 인용 품질. 정답 구간이 없으면 f1·ratio는 None."""
    n, row = o["nano"], o["row"]
    q = html.unescape(str(n.get("answer_quote") or ""))
    gold = list(row.labels.get("unsupported") or [])
    glen = len(N("".join(gold)))
    return {
        "ok": o["rec"].get("answer_quote_ok"),
        "literal": bool(q.strip()) and q in row.candidate_answer,
        "kb_empty": not str(n.get("kb_quote") or "").strip(),
        "ratio": len(N(q)) / glen if gold and glen else None,
        "f1": scoring.span_f1([q] if q.strip() else [], gold) if gold else None,
    }


def med(xs) -> float:
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else float("nan")


def sec_q(ctx: dict, runs: dict, top: int) -> None:
    out("\n== q. 잡았을 때의 인용 품질(정답 근거 없음 행, --runs) ==")
    obs = collect(ctx, runs)
    tg = {i: os_ for i, os_ in obs.items() if gold_ungrounded(os_[0]["row"])}
    flat = [o for os_ in tg.values() for o in os_ if says_ungrounded(o) is not None]
    caught = [o for o in flat if says_ungrounded(o)]
    missed = [o for o in flat if not says_ungrounded(o)]
    out(f"정답 근거 없음 행 {len(tg)}개, nano 판정 {len(flat)}건 = 잡음 {len(caught)} + 놓침 {len(missed)}"
        f" (판정 없음·파싱 실패 {sum(len(v) for v in tg.values()) - len(flat)}건)")  # fmt: skip
    if not caught:
        out("잡은 건이 없어 인용 품질을 못 낸다")
        return
    qm = [quote_metrics(o) for o in caught]
    ok = [m["ok"] for m in qm if m["ok"] is not None]
    f1 = [m["f1"] for m in qm if m["f1"] is not None]
    ra = [m["ratio"] for m in qm if m["ratio"] is not None]
    out(f"answer_quote_ok 참 비율 {mean(ok):.1%}(기록 있는 {len(ok)}건) | 인용이 원문에 글자 그대로 {mean(m['literal'] for m in qm):.1%}"
        f" | kb_quote가 빔 {mean(m['kb_empty'] for m in qm):.1%}")  # fmt: skip
    out(f"인용 길이/정답 구간 길이(정규화 문자) 중앙값 {fmt(pct(ra, .5))} 사분위 {fmt(pct(ra, .25))}–{fmt(pct(ra, .75))} (n={len(ra)})")
    out(f"인용 구간 F1 중앙값 {fmt(pct(f1, .5))}, 평균 {fmt(mean(f1))}, F1 < 0.5 비율 {mean(x < 0.5 for x in f1) if f1 else float('nan'):.1%} (n={len(f1)})")  # fmt: skip
    out(f"\n{'':<8}{'건수':>5}{'completion_tokens 중앙값':>26}{'latency_ms 중앙값':>20}{'finish_reason':>16}")
    for name, g in (("잡은 건", caught), ("놓친 건", missed)):
        cs = [o["call"] for o in g if o["call"]]
        tok, lat = [c.get("completion_tokens") for c in cs], [c.get("latency_ms") for c in cs]
        fr = dist(Counter(str(c.get("finish_reason")) for c in cs))
        out(f"{name:<8}{len(g):>5}{fmt(med(tok), 26, 1)}{fmt(med(lat), 20, 0)}  {fr}")
    out("읽는 법: 놓친 건의 토큰이 눈에 띄게 적으면 놓칠 때 덜 생각한다는 신호(표본 작으면 참고만)")
    out("\n문항별(정답 근거 없음 행)  id  잡은/n  평균 F1(잡은 건)  평균 인용길이/정답길이")
    for i, os_ in sorted(tg.items(), key=lambda x: (sum(bool(says_ungrounded(o)) for o in x[1]) / len(x[1]), x[0]))[:top]:
        nj = [o for o in os_ if says_ungrounded(o) is not None]
        cg = [o for o in nj if says_ungrounded(o)]
        m = [quote_metrics(o) for o in cg]
        out(f"  {i[:8]:<9}{len(cg):>3}/{len(nj):<3}{fmt(mean(x['f1'] for x in m if x['f1'] is not None) if m else float('nan'), 12)}"
            f"{fmt(mean(x['ratio'] for x in m if x['ratio'] is not None) if m else float('nan'), 16)}")  # fmt: skip


# ---------------- s. 문장별 모드 재생 ----------------
def clean_checks(nano: dict | None) -> list[dict] | None:
    """parsed.checks에서 모양이 맞는 항목만. checks가 목록이 아니면 None."""
    cs = (nano or {}).get("checks")
    if not isinstance(cs, list):
        return None
    return [c for c in cs if isinstance(c, dict) and isinstance(c.get("n"), int) and not isinstance(c["n"], bool)]


def variants(nano: dict, shown: int, kb_has, ans_ok: bool) -> dict[str, bool]:
    """판정 변형 → 근거 없음 여부. shown = nano에 보여 준 문장 수(그 밖의 n은 무시)."""
    chk = [c for c in clean_checks(nano) or [] if 1 <= c["n"] <= shown]
    conf = [c for c in chk if c.get("verdict") == "conflicts"]
    nim = [c for c in chk if c.get("verdict") == "not_in_material"]
    v3 = any(kb_has(str(c.get("kb_phrase") or "")) for c in conf)
    v0 = not nano.get("grounded", True)
    return {"v0": v0, "v1": bool(conf or nim), "v1o": v0 or bool(conf or nim), "v2": bool(conf),
            "v3": v3, "v4": v3 and bool(ans_ok), "v5": bool(nim)}  # fmt: skip


def flagged_overlap(answer: str, gold: list[str], chk: list[dict], shown: int) -> dict | None:
    """flagged 문장 범위와 정답 구간의 겹침. {verdict: (건수, 겹친 건수)}. 정답 구간을 못 찾으면 None."""
    G = s0.locate(answer, gold)
    if G is None or not gold:
        return None
    cuts, res = sentences(answer), {}
    for c in chk:
        v = c.get("verdict")
        if v in ("conflicts", "not_in_material") and 1 <= c["n"] <= min(shown, len(cuts)):
            t, h = res.get(v, (0, 0))
            res[v] = (t + 1, h + (s0.overlap(cuts[c["n"] - 1], G) > 0))
    return res


def make_kb_has(cfg) -> tuple:
    """(kb_phrase가 KB 전체 섹션 글에 있는가 함수, 안내). KB를 못 읽으면 빈 문자열이 아님으로 대체."""
    try:
        from guardrail.kb import KbIndex

        kb = KbIndex.load(cfg.paths.kb, ngram=cfg.kb.ngram, k1=cfg.kb.k1, b=cfg.kb.b)
        text = "\n".join(kb.section_text(s) for s in dict.fromkeys(sid for sid, _ in kb.chunks))
    except Exception as e:  # noqa: BLE001  (원격 환경 차이로 KB를 못 읽는 경우를 알리고 대체)
        return (lambda p: bool(p.strip())), f"KB 전체 글을 못 얻어 kb_phrase가 빈 문자열이 아님으로 대체({type(e).__name__})"
    cached = lru_cache(maxsize=None)(lambda p: quote_ok(p, text, KB_MIN_CHARS))
    return cached, f"KB 전체 섹션 글 {len(text)}자에서 quote_ok(kb_phrase, ·, {KB_MIN_CHARS})로 확인"


def level_a(obs: dict[str, list[dict]], n_runs: int) -> dict | None:
    """--runs의 같은 수준 값: 인용 모드 nano 판정만(섹션 p의 (a))."""
    miss = fa = tot = 0
    for os_ in obs.values():
        for o in os_:
            u = says_ungrounded(o)
            if u is None:
                continue
            tot += 1
            g = gold_ungrounded(o["row"])
            miss += g and not u
            fa += (not g) and u
    return {"miss": miss / n_runs, "fa": fa / n_runs, "acc": 1 - (miss + fa) / tot, "n": tot} if tot else None


def sec_s(ctx: dict, sruns: dict, runs: dict | None, cfg) -> None:
    out("\n== s. 문장별 모드 재생(--sent-runs) ==")
    obs = collect(ctx, sruns)
    kb_has, note = make_kb_has(cfg)
    out(note)
    states = Counter(o["state"] for os_ in obs.values() for o in os_)
    nochk = Counter()
    rows = []  # 변형 계산이 된 관측
    for os_ in obs.values():
        for o in os_:
            if o["state"] != "있음":
                continue
            if clean_checks(o["nano"]) is None or not isinstance(o["nano"].get("grounded"), bool):
                nochk["checks 없음·grounded가 bool 아님"] += 1
                continue
            a = o["row"].candidate_answer
            nrev, nraw = len(sentences(reveal_hidden(a))), len(sentences(a))
            shown = min(nrev, MAX_SENTENCES)
            ao = o["rec"].get("answer_quote_ok")
            if ao is None:
                ao = quote_ok(str(o["nano"].get("answer_quote") or ""), a, 8)
            rows.append({**o, "shown": shown, "nraw": nraw, "nrev": nrev,
                         "v": variants(o["nano"], shown, kb_has, bool(ao))})  # fmt: skip
    nrun = len(sruns)
    out(f"관측 {sum(states.values())}건, nano 상태 {dist(states)}(건수/회 {states['파싱실패'] / nrun:.2f} 파싱 실패,"
        f" {states['없음'] / nrun:.2f} 호출 없음), 변형 계산 {len(rows)}건, 제외 {dist(nochk)}")  # fmt: skip
    if not rows:
        out("계산할 수 있는 문장 모드 기록이 없다")
        return
    ref = level_a(collect(ctx, runs), len(runs)) if runs else None
    out(f"\n{'변형':<5}{'설명':<36}{'놓침/회':>8}{'오경보/회':>10}{'정확도×100':>11}  Δ정확도(인용 모드 (a) 대비, 점)")
    stat = {}
    for v in VARIANTS:
        miss = sum(gold_ungrounded(o["row"]) and not o["v"][v] for o in rows)
        fa = sum((not gold_ungrounded(o["row"])) and o["v"][v] for o in rows)
        acc = 1 - (miss + fa) / len(rows)
        stat[v] = (miss / nrun, fa / nrun, acc)
        d = f"{100 * (acc - ref['acc']):+.2f}" if ref else "(--runs 없음)"
        out(f"{v:<5}{VNOTE[v]:<36}{miss / nrun:8.2f}{fa / nrun:10.2f}{100 * acc:11.2f}  {d}")
    if ref:
        out(f"인용 모드 (a): 놓침 {ref['miss']:.2f}/회, 오경보 {ref['fa']:.2f}/회, 정확도 {100 * ref['acc']:.2f}(관측 {ref['n']}건, 실행 {len(runs)}개)")
    else:
        out("--runs가 없어 인용 모드와의 차이는 생략")
    out("\n오경보를 낸 정답 근거 있음 행: 변형별 상위 10(id 횟수/문장 모드 관측 수)")
    for v in VARIANTS:
        c = Counter(o["row"].id for o in rows if not gold_ungrounded(o["row"]) and o["v"][v])
        tot = Counter(o["row"].id for o in rows)
        out(f"  {v:<4}" + (" ".join(f"{i[:8]}:{k}/{tot[i]}" for i, k in c.most_common(10)) or "없음"))
    # verdict별 정밀도: 정답 근거 없음 행에서 flagged 문장이 정답 구간과 겹치는 비율
    agg, mism, unloc, nflag_rows, hit_rows = {}, 0, 0, 0, 0
    for o in rows:
        r = o["row"]
        if not gold_ungrounded(r):
            continue
        chk = clean_checks(o["nano"])
        if o["nrev"] != o["nraw"] or any(c["n"] > o["shown"] for c in chk):
            mism += 1
            continue
        res = flagged_overlap(r.candidate_answer, list(r.labels.get("unsupported") or []), chk, o["shown"])
        if res is None:
            unloc += 1
            continue
        if res:
            nflag_rows += 1
            hit_rows += any(h for _, h in res.values())
        for k, (t, h) in res.items():
            a0 = agg.get(k, (0, 0))
            agg[k] = (a0[0] + t, a0[1] + h)
    out(f"\nverdict 정밀도(정답 근거 없음 행, 문장 수 불일치로 제외 {mism}건, 정답 구간 못 찾음 {unloc}건)")
    for k in ("conflicts", "not_in_material"):
        t, h = agg.get(k, (0, 0))
        out(f"  {k:<16} flagged {t}건 중 정답 구간과 겹침 {h}건 = {h / t if t else float('nan'):.1%}")
    out(f"  flagged가 있는 행 {nflag_rows}건 중 하나라도 정답 구간과 겹침 {hit_rows}건 = {hit_rows / nflag_rows if nflag_rows else float('nan'):.1%}")
    cnt = Counter()
    for o in rows:
        if not gold_ungrounded(o["row"]):
            for c in clean_checks(o["nano"]):
                if 1 <= c["n"] <= o["shown"] and c.get("verdict") in ("conflicts", "not_in_material"):
                    cnt[c["verdict"]] += 1
    out("정답 근거 있음 행에서 나온 건수/회: " + ", ".join(f"{k} {cnt[k] / nrun:.2f}" for k in ("conflicts", "not_in_material")))
    out("\n문장 수(원문 답변) 분포와, 정답 근거 있음 관측의 변형별 오경보율")
    out(f"{'문장수':<7}{'관측':>6}{'정답있음 관측':>14}" + "".join(f"{v:>8}" for v in VARIANTS))
    for b in ("1", "2", "3", "4+"):
        inb = [o for o in rows if (str(o["nraw"]) if o["nraw"] < 4 else "4+") == b]
        gg = [o for o in inb if not gold_ungrounded(o["row"])]
        out(f"{b:<7}{len(inb):>6}{len(gg):>14}" + "".join(f"{mean(o['v'][v] for o in gg) if gg else float('nan'):8.3f}" for v in VARIANTS))
    out("한계: 변형은 nano 판정 수준만(② 단·인용 검증 이후 구간 점수 제외). 실행 횟수가 적으면 문항별 횟수 표는 잡음이 크다")


# ---------------- 자체 검사 ----------------
def selftest() -> None:
    close = lambda x, y: abs(x - y) < 1e-9  # noqa: E731
    # 정답 근거 없음 p=.5, 정답 근거 있음 p=.2: 손 계산. 놓침 = 1 - detect, 오경보 = detect
    sim = simulate([(True, 0.5), (False, 0.2)])
    want = {  # 방식: (놓침, 오경보, 호출)
        "a": (0.5, 0.2, 2.0), "b": (0.25, 0.36, 4.0), "c": (0.75, 0.04, 4.0),
        "d": (0.5, 3 * 0.04 * 0.8 + 0.008, 6.0), "e": (0.25, 0.36, 1.5 + 1.8),
    }  # fmt: skip
    for s, (m, f, c) in want.items():
        pc.check(close(sim[s]["miss"], m) and close(sim[s]["fa"], f) and close(sim[s]["calls"], c), (s, sim[s]))
    pc.check(close(sim["a"]["acc"], 1 - 0.7 / 2), sim["a"])
    pc.check([bin_of(x) for x in (0, 0.2, 0.3, 0.8, 0.9, 1)] == list(BINS), "bin_of")
    # 변형: shown=2, n=5는 무시. kb_has는 "자료문구"만 참
    kb = lambda s: s == "자료문구"  # noqa: E731
    mk = lambda g, *cs: {"grounded": g, "checks": [{"n": n, "kb_phrase": p, "verdict": v} for n, p, v in cs]}  # noqa: E731
    base = dict.fromkeys(VARIANTS, False)
    nano = mk(True, (1, "자료문구", "conflicts"), (2, "", "not_in_material"), (5, "자료문구", "conflicts"))
    pc.check(variants(nano, 2, kb, False) == {**base, "v1": True, "v1o": True, "v2": True, "v3": True, "v5": True}, "v-a")
    pc.check(variants(nano, 2, kb, True)["v4"] is True, "v4 통과")
    nano = mk(False, (2, "없는 구절", "conflicts"), (1, "x", "matches"))
    pc.check(variants(nano, 2, kb, True) == {**base, "v0": True, "v1": True, "v1o": True, "v2": True}, "v-b")
    nano = mk(True, (3, "자료문구", "conflicts"))  # 보여 준 문장 밖
    pc.check(variants(nano, 2, kb, True) == base, "v-c")
    # 겹침: 두 번째 문장 "라마바 사."만 정답 구간 "라마바"와 겹친다
    a = "가나다 나. 라마바 사. 아자차."
    chk = [{"n": 1, "verdict": "conflicts"}, {"n": 2, "verdict": "conflicts"}, {"n": 3, "verdict": "not_in_material"}]
    pc.check(flagged_overlap(a, ["라마바"], chk, 3) == {"conflicts": (2, 1), "not_in_material": (1, 0)}, "overlap")
    pc.check(flagged_overlap(a, ["없는구간"], chk, 3) is None, "overlap 못 찾음")
    pc.check(clean_checks({"checks": [{"n": 1}, {"n": "2"}, 3]}) == [{"n": 1}] and clean_checks({}) is None, "checks 정리")
    out("selftest ok")


def main() -> None:
    ap = argparse.ArgumentParser(description="출력 판정 놓침 캐기(API 0회): p 확률·모의, q 인용 품질, s 문장별 모드 재생")
    ap.add_argument("--runs", nargs="+", metavar="DIR", default=[], help="인용 모드(기본 출력 판정) 실행 폴더")
    ap.add_argument("--sent-runs", nargs="+", metavar="DIR", default=[], help="output.judge_mode: sentences 실행 폴더")
    ap.add_argument("--section", choices=("p", "q", "s", "all"), default="all")
    ap.add_argument("--config", default="configs/default.yaml")
    ap.add_argument("--config-name")
    ap.add_argument("--top", type=int, default=40, help="문항 표에 찍을 최대 행 수")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not (args.runs or args.sent_runs):
        ap.error("--runs 또는 --sent-runs가 필요하다")
    ctx = pc.make_ctx(args.config)
    keep = {r.id for r in ctx["rows"]}
    runs, sruns = {}, {}
    for dirs, dst in ((args.runs, runs), (args.sent_runs, sruns)):
        if dirs:
            got, notes = s0.read_recs(dirs, keep, args.config_name)
            dst.update(got)
            for n in notes:
                out(n)
    out(f"문항 {len(ctx['rows'])}개(홀드아웃 제외), 출력 행 {sum(r.candidate_answer is not None for r in ctx['rows'])}개,"
        f" 인용 모드 실행 {len(runs)}개, 문장 모드 실행 {len(sruns)}개")  # fmt: skip
    sec = args.section
    if sec in ("p", "all"):
        sec_p(ctx, runs, args.top) if runs else out("\n(p 생략: --runs 없음)")
    if sec in ("q", "all"):
        sec_q(ctx, runs, args.top) if runs else out("\n(q 생략: --runs 없음)")
    if sec in ("s", "all"):
        if sruns:
            from guardrail.config import load_config, resolve_path

            sec_s(ctx, sruns, runs or None, load_config(resolve_path(args.config)))
        else:
            out("\n(s 생략: --sent-runs 없음)")


if __name__ == "__main__":
    main()
