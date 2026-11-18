"""S3.13 0단계: 판정기 개선 후보를 LLM 호출 0회로 거르는 분석(전체 경로 기록 + 공개 세트 정답만 읽는다).

실행(guardrail/에서):
`uv run python -X utf8 scripts/r17/stage0.py --runs runs/A runs/B [--section a|d|f|items|all] [--config-name NAME]`
`uv run python -X utf8 scripts/r17/stage0.py --selftest`

- a: 절 번호 방식의 상한(오라클). 문장·절·어절 묶음(≤4어절)별로 "닿는 단위 전부"·"최적 부분집합" 구간 F1.
- d: nano와 kNN·LR이 갈리는 행(2차 호출을 걸면 최대로 고칠/망칠 수).
- f: 호출 상태(role별 finish_reason·오류·재시도·토큰·지연, 요청당 호출 수).
- items: 문항별 오답표(차단 오답, 근거 판정 놓침, 반대 방향, tier 3 구간 F1이 낮은 문항).
- 실행 하나 = (폴더, config, repeat). 기록 선별은 paired_compare와 같다(guard 행, 홀드아웃 제외, invalid·단독 폴더 제외).
- 문항 문구는 출력하지 않는다(id 앞 8자·숫자·라벨만).
- 기록 필드 가정(원격에서 틀릴 수 있음): llm[].role ∈ input_judge/jb_check/oom_check/output_judge, parsed.category(입력),
  parsed.grounded(출력), tier(2 = 규칙, 3 = nano 인용), calls, knn_major, lr_pred, answer_quote_ok.
- shortcut: 후보 답변은 공개 세트 원문을 쓴다. Guard가 긴 답변을 자르면 위치가 어긋날 수 있다.
"""

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "r16")]
import paired_compare as pc  # noqa: E402
import span_replay as sr  # noqa: E402
from guardrail.eval.score import scoring  # noqa: E402

N = scoring.normalize
OUT_W = 1 - scoring.GUARD_DECISION_WEIGHT - scoring.GUARD_CATEGORY_WEIGHT  # 출력 성분 가중치(0.45)
N_OUT_PARTS = 3  # 출력 성분: 근거, 구간, safe
SECOND = ("jb_check", "oom_check")
INPUT_SIDE = ("input_judge", *SECOND)
ROLES = ("input_judge", "jb_check", "oom_check", "output_judge")
UNITS = ("문장", "절", "어절묶음")
WINDOW = 4


def out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def pct(xs, q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def dist(c: Counter) -> str:
    return ",".join(f"{k}:{v}" for k, v in c.most_common()) or "-"


# ---------------- 기록 읽기 ----------------
def read_recs(dirs: list[str], keep: set[str], config_name: str | None = None):
    """{(폴더, config, repeat): {id: 기록 전체}}과 안내 줄. paired_compare.read_runs와 같은 제외 규칙."""
    runs: dict[tuple, dict[str, dict]] = {}
    notes = []
    for d in dirs:
        p = Path(d)
        p = p.parent if p.is_file() else p
        sm = p / "summary.json"
        if sm.exists():
            s = json.loads(sm.read_text("utf-8"))
            if s.get("invalid"):
                notes.append(f"제외 {p.name}: invalid")
                continue
            if any(s.get(k) for k in ("input_only", "output_only")):
                notes.append(f"제외 {p.name}: 입력·출력 단독 실행")
                continue
        else:
            notes.append(f"경고 {p.name}: summary.json 없음")
        path = p / "responses.jsonl"
        pc.check(path.exists(), f"{path}가 없다")
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if config_name is not None and rec.get("config", "") != config_name:
                continue
            if rec["id"] in keep:
                runs.setdefault((p.name, rec.get("config", ""), rec["repeat"]), {})[rec["id"]] = rec
    return runs, notes


def parsed(rec: dict, has_out: bool):
    """채점기와 같은 응답 검사. 무효·누락이면 None."""
    resp = rec.get("response")
    return scoring.parse_guard(resp, has_out) if resp is not None else None


def calls_of(rec: dict, *roles: str) -> list[dict]:
    return [c for c in (rec.get("llm") or []) if c.get("role") in roles]


def tier_name(rec: dict) -> str:
    return {"2": "2(규칙)", "3": "3(nano 인용)"}.get(str(rec.get("tier")), "기타")


# ---------------- a. 절 번호 오라클 ----------------
def clauses(a: str) -> list[tuple[int, int]]:
    """문장을 쉼표·쌍점·연결어미 어절 뒤에서 더 쪼갠 절(분할)."""
    res = []
    for s, e in sr.sentences(a):
        cuts = [s]
        for m in re.finditer(r"\S+", a[s:e]):
            end = s + m.end()
            if end >= e:
                break
            w = m.group()
            core = w.rstrip(",;:")
            if w != core or sr.CONN.search(core):
                cuts.append(end)
        cuts.append(e)
        for x, y in zip(cuts, cuts[1:], strict=False):
            while x < y and a[x].isspace():
                x += 1
            if x < y:
                res.append((x, y))
    return res


def windows(a: str, cls: list[tuple[int, int]], k: int = WINDOW) -> list[tuple[int, int]]:
    """절을 최대 k어절 창으로 쪼갠다."""
    res = []
    for s, e in cls:
        ws = [(s + m.start(), s + m.end()) for m in re.finditer(r"\S+", a[s:e])]
        res += [(ws[i][0], ws[min(i + k, len(ws)) - 1][1]) for i in range(0, len(ws), k)]
    return res


def prf(pred, gold) -> tuple[float, float, float]:
    """채점기 span_f1과 같은 문자 단위 (정밀도, 재현율, F1)."""
    p, g = Counter(N("".join(pred))), Counter(N("".join(gold)))
    if not p and not g:
        return 1.0, 1.0, 1.0
    o = sum((p & g).values())
    if not o:
        return 0.0, 0.0, 0.0
    pr, rc = o / sum(p.values()), o / sum(g.values())
    return pr, rc, 2 * pr * rc / (pr + rc)


def locate(a: str, gold: list[str]) -> list[tuple[int, int]] | None:
    """정답 구간 문자열의 답변 안 위치. 하나라도 못 찾으면 None."""
    res = []
    for g in gold:
        i = a.find(g)
        if i < 0:
            return None
        res.append((i, i + len(g)))
    return res


def overlap(u: tuple[int, int], G: list[tuple[int, int]]) -> int:
    return sum(max(0, min(u[1], ge) - max(u[0], gs)) for gs, ge in G)


def oracle(a: str, units, G, gold):
    """(닿는 단위 전부 PRF, 최적 부분집합 PRF). 부분집합 = 겹침 비율 큰 순으로 하나씩 더해 F1 최대 지점."""
    ovs = [overlap(u, G) for u in units]
    text = lambda idx: [a[units[i][0] : units[i][1]] for i in idx]  # noqa: E731
    touch = prf(text([i for i, o in enumerate(ovs) if o > 0]), gold)
    order = sorted(range(len(units)), key=lambda i: (-ovs[i] / (units[i][1] - units[i][0]), i))
    best = (0.0, 0.0, 0.0)
    for n in range(1, len(order) + 1):
        r = prf(text(order[:n]), gold)
        if r[2] > best[2]:
            best = r
    return touch, best


def shape(a: str, cls, G, gold) -> list[tuple[int, bool, bool, float]]:
    """정답 구간마다 (길이, 절과 정확히 일치, 한 절 안, 절 길이 대비 정답 길이 비)."""
    res = []
    for g, (gs, ge) in zip(gold, G, strict=True):
        best = max(cls, key=lambda u: overlap(u, [(gs, ge)]), default=None)
        exact = any(N(g) == N(a[s:e]) for s, e in cls)
        inside = any(s <= gs and ge <= e for s, e in cls)
        ratio = len(N(g)) / max(1, len(N(a[best[0] : best[1]]))) if best else float("nan")
        res.append((len(g), exact, inside, ratio))
    return res


def sec_a(ctx: dict, runs: dict) -> None:
    out("\n== a. 절 번호 방식의 상한(오라클) ==")
    rows = ctx["rows"]
    n_out = sum(r.candidate_answer is not None for r in rows)
    tgt = {r.id: r for r in rows if r.candidate_answer is not None and not r.labels["grounded"]
           and r.labels["unsupported"]}  # fmt: skip
    orc, shp, unloc = {}, [], 0
    for i, r in tgt.items():
        a, gold = r.candidate_answer, list(r.labels["unsupported"])
        G = locate(a, gold)
        if G is None:
            unloc += 1
            continue
        cls = clauses(a)
        us = {"문장": sr.sentences(a), "절": cls, "어절묶음": windows(a, cls)}
        orc[i] = {k: (len(u), *oracle(a, u, G, gold)) for k, u in us.items()}
        shp += shape(a, cls, G, gold)
    out(f"대상 행(정답 근거 없음 + 정답 구간 있음) {len(tgt)}개, 정답 구간을 답변에서 못 찾아 제외 {unloc}개, 분석 {len(orc)}개")
    if not orc:
        return
    out(f"\n{'단위':<9}{'단위수/답변':>10} | 닿는 전부 P / R / F1 | 최적 부분집합 P / R / F1")
    for k in UNITS:
        v = [o[k] for o in orc.values()]
        t = [mean(x[1][j] for x in v) for j in range(3)]
        b = [mean(x[2][j] for x in v) for j in range(3)]
        out(f"{k:<9}{mean(x[0] for x in v):10.2f} |{t[0]:8.3f}{t[1]:7.3f}{t[2]:7.3f} |{b[0]:9.3f}{b[1]:7.3f}{b[2]:7.3f}")
    L = [s[0] for s in shp]
    ra = [s[3] for s in shp if not math.isnan(s[3])]
    out(
        f"\n정답 구간 {len(shp)}개: 길이(문자) 중앙값 {pct(L, .5):.0f} 사분위 {pct(L, .25):.0f}–{pct(L, .75):.0f}"
        f" | 절과 정확히 일치 {mean(s[1] for s in shp):.1%} | 한 절 안 {mean(s[2] for s in shp):.1%}"
        f" | 절 길이 대비 정답 길이 비 중앙값 {pct(ra, .5):.3f}"
    )
    # 기록의 실제 구간 F1(실행마다 행마다), tier별
    pairs = []
    for run, recs in runs.items():
        for i in orc:
            rec = recs.get(i)
            if rec is None:
                continue
            p = parsed(rec, True)
            f1 = scoring.span_f1(p["output"]["unsupported"], tgt[i].labels["unsupported"]) if p else 0.0
            pairs.append((run, i, tier_name(rec), f1))
    tiers = sorted({p[2] for p in pairs})
    out(f"\n기록의 실제 구간 F1(같은 행들, 실행 {len(runs)}개 × 행): 절 '닿는 전부' 오라클을 같은 행·같은 횟수로 나란히")
    out(f"{'tier':<14}{'건수':>6}{'실제 F1':>9}{'오라클(절 닿는 전부)':>20}")
    for t in (*tiers, "전체"):
        ps = [p for p in pairs if t == "전체" or p[2] == t]
        out(f"{t:<14}{len(ps):>6}{mean(p[3] for p in ps):9.3f}{mean(orc[p[1]]['절'][1][2] for p in ps):20.3f}")
    t3 = [p for p in pairs if p[2].startswith("3")]
    if not t3:
        out("\ntier 3 행이 기록에 없다(tier 필드가 없거나 nano 인용이 안 쓰였다) — 판정 생략")
        return
    out(f"\ntier 3 행만({len(t3)}건) 실제 nano 인용 F1 {mean(p[3] for p in t3):.3f} 대 오라클")
    for k in UNITS:
        out(
            f"  {k:<9} 닿는 전부 {mean(orc[p[1]][k][1][2] for p in t3):.3f}"
            f"  최적 부분집합 {mean(orc[p[1]][k][2][2] for p in t3):.3f}"
        )
    act, ora = mean(p[3] for p in t3), mean(orc[p[1]]["절"][1][2] for p in t3)
    per_run = [sum(orc[p[1]]["절"][1][2] - p[3] for p in t3 if p[0] == r) for r in runs]
    kk = len(t3) / len(runs)
    # 가드 점수 = 100 × 출력 가중치(0.45) × (해당 행 수 / 출력 행 수) × (행 평균 F1 차) / 출력 성분 수(3). 가중치 합은 1(범주 채점 행 있음)로 본다
    pts = 100 * OUT_W * (kk / n_out) * (ora - act) / N_OUT_PARTS
    out(
        f"판정: 절 '닿는 전부' 오라클 {ora:.3f} {'>' if ora > act else '≤'} tier 3 실제 {act:.3f},"
        f" 차이 {100 * (ora - act):+.1f}점(구간 F1×100) → 가드 점수 {pts:+.3f}점"
        f" (= 차이 × {OUT_W:.2f} × ({kk:.1f}행/회 ÷ 출력 {n_out}행) ÷ {N_OUT_PARTS} × 100;"
        f" 실행별 합산 평균 {100 * OUT_W * mean(per_run) / n_out / N_OUT_PARTS:+.3f})"
    )


# ---------------- d. nano와 kNN·LR이 갈리는 행 ----------------
DEFS = {
    "(i) nano≠kNN": lambda n, k, ll: k is not None and n != k,
    "(ii) nano≠LR": lambda n, k, ll: ll is not None and n != ll,
    "(iii) kNN·LR 둘 다 반대": lambda n, k, ll: k is not None and ll is not None and k == ll != n,
    "참고: 불일치 무관 전체": lambda n, k, ll: True,
}


def disagree_table(views: list[dict], n_runs: int) -> dict[str, dict[str, float]]:
    """2차 호출을 안 쓴 행만, 불일치 정의별 회당 평균 {miss: 놓침, fp: 잘못 막음, ok: 정답}.
    view = {nano, knn, lr: 차단이면 True·없으면 None, gold, pred: 차단이면 True, second}."""
    res = {k: Counter() for k in DEFS}
    for v in views:
        if v["second"] or v["nano"] is None:
            continue
        kind = "ok" if v["gold"] == v["pred"] else ("miss" if v["gold"] else "fp")
        for k, f in DEFS.items():
            if f(v["nano"], v["knn"], v["lr"]):
                res[k][kind] += 1
    return {k: {x: c[x] / n_runs for x in ("miss", "fp", "ok")} for k, c in res.items()}


def view_of(rec: dict, row, block) -> dict | None:
    p = parsed(rec, row.candidate_answer is not None)
    if p is None:
        return None
    isb = lambda c: None if c is None else c in block  # noqa: E731
    nano = next((c["parsed"]["category"] for c in calls_of(rec, "input_judge") if c.get("parsed")), None)
    return {
        "nano": isb(nano), "nano_cat": nano, "knn": isb(rec.get("knn_major")), "lr": isb(rec.get("lr_pred")),
        "gold": row.labels["decision"] == "block", "pred": p["input"]["decision"] == "block",
        "pred_cat": p["input"]["category"], "second": bool(calls_of(rec, *SECOND)),
    }  # fmt: skip


def all_views(ctx: dict, runs: dict) -> tuple[list[dict], int]:
    """(행 보기 목록(id·run 포함), 무효 응답 수)"""
    views, bad = [], 0
    for run, recs in runs.items():
        for r in ctx["rows"]:
            rec = recs.get(r.id)
            v = view_of(rec, r, ctx["block"]) if rec else None
            if v is None:
                bad += rec is not None
                continue
            views.append({**v, "run": run, "id": r.id})
    return views, bad


def sec_d(ctx: dict, runs: dict) -> None:
    out("\n== d. kNN·LR과 nano가 갈리는 행 ==")
    views, bad = all_views(ctx, runs)
    n = len(runs)
    out(
        f"보기 {len(views)}건(실행 {n}개), 무효 응답 {bad}건 | nano 없음 {sum(v['nano'] is None for v in views)}"
        f" | kNN 없음 {sum(v['knn'] is None for v in views)} | LR 없음 {sum(v['lr'] is None for v in views)}"
        f" | 2차 호출 사용 {sum(v['second'] for v in views)}"
    )
    t = disagree_table(views, n)
    out("2차 호출을 안 쓴 행만, 회당 평균 건수(nano 없는 행 제외). 오답 = 최종 decision ≠ 정답")
    out(f"{'불일치 정의':<24}{'놓침':>8}{'잘못 막음':>10}{'오답 합':>9}{'정답':>9}")
    for k, v in t.items():
        out(f"{k:<24}{v['miss']:8.2f}{v['fp']:10.2f}{v['miss'] + v['fp']:9.2f}{v['ok']:9.2f}")
    out("읽는 법: 오답 합 = 불일치일 때 2차 판정으로 최대 고칠 수 있는 오답 수, 정답 = 새로 망칠 위험이 있는 정답 수")


# ---------------- f. 호출 상태 ----------------
def sec_f(ctx: dict, runs: dict) -> None:
    out("\n== f. 호출 상태 ==")
    recs = [(run, r) for run, d in runs.items() for r in d.values()]
    empty = sum(not r.get("llm") for _, r in recs)
    out(f"기록 {len(recs)}건, llm 목록이 빈 기록 {empty}건, 실행 {len(runs)}개")
    out(f"{'role':<14}{'회/실행':>8}  finish_reason | 오류율 재시도율 | 토큰 p50/p95/max | 지연ms p50/p95/max")
    for role in ROLES:
        cs = [c for _, r in recs for c in calls_of(r, role)]
        if not cs:
            out(f"{role:<14}{0:>8}  (호출 없음)")
            continue
        tok = [c["completion_tokens"] for c in cs if c.get("completion_tokens") is not None]
        lat = [c["latency_ms"] for c in cs if c.get("latency_ms") is not None]
        fr = Counter(str(c.get("finish_reason")) for c in cs)
        out(
            f"{role:<14}{len(cs) / len(runs):8.1f}  {dist(fr)} | {mean(bool(c.get('error')) for c in cs):.3f}"
            f" {mean((c.get('attempts') or 1) > 1 for c in cs):.3f} |"
            f" {pct(tok, .5):.0f}/{pct(tok, .95):.0f}/{max(tok, default=float('nan')):.0f} |"
            f" {pct(lat, .5):.0f}/{pct(lat, .95):.0f}/{max(lat, default=float('nan')):.0f}"
        )
    cc = Counter(r.get("calls") for _, r in recs)
    out(f"요청당 calls 분포(전체 기록): {dist(Counter({str(k): v for k, v in cc.items()}))}")
    has_out = {r.id for r in ctx["rows"] if r.candidate_answer is not None}
    orows = [r for d in runs.values() for i, r in d.items() if i in has_out and r.get("llm")]
    if orows:
        one = sum(len(calls_of(r, *INPUT_SIDE)) == 1 for r in orows)
        out(f"출력 행 중 입력 쪽 호출이 1회뿐 {one}/{len(orows)} = {one / len(orows):.1%} (출력 쪽에 호출 여유가 있는 행)")
    else:
        out("출력 행 중 llm 기록이 있는 행이 없어 입력 쪽 1회 비율을 못 낸다")


# ---------------- items. 문항별 오답표 ----------------
def sec_items(ctx: dict, runs: dict, top: int) -> None:
    out("\n== items. 문항별 오답표 ==")
    rows = {r.id: (j, r) for j, r in enumerate(ctx["rows"])}
    nrun = Counter(i for d in runs.values() for i in d)
    views, _ = all_views(ctx, runs)
    byid: dict[str, list[dict]] = {}
    for v in views:
        byid.setdefault(v["id"], []).append(v)
    wrong = {i: [v for v in vs if v["gold"] != v["pred"]] for i, vs in byid.items()}
    wrong = {i: w for i, w in wrong.items() if w}
    out(f"(1) 차단 오답 문항 {len(wrong)}개(오답 많은 순 {top}개)")
    out("id       군집      정답                       오답/실행  오답일 때 예측 범주 | nano 1차 범주 | 2차가 뒤집음")
    for i, w in sorted(wrong.items(), key=lambda x: -len(x[1]))[:top]:
        j, r = rows[i]
        flips = sum(v["second"] and v["nano"] is not None and v["pred"] != v["nano"] for v in w)
        out(
            f"{i[:8]:<9}{ctx['names'][ctx['cl'][j]][:8]:<10}{r.labels['decision'] + '/' + r.labels['category']:<27}"
            f"{len(w):>3}/{nrun[i]:<6} {dist(Counter(v['pred_cat'] for v in w))} | "
            f"{dist(Counter(str(v['nano_cat']) for v in w))} | {flips}"
        )
    miss, opp, t3 = {}, 0, {}
    cls = Counter()
    for run, d in runs.items():
        for i, rec in d.items():
            j, r = rows[i]
            if r.candidate_answer is None:
                continue
            p = parsed(rec, True)
            if p is None:
                continue
            g = r.labels["grounded"]
            if not g and p["output"]["grounded"]:
                oj = [c for c in calls_of(rec, "output_judge")]
                ng = next((c["parsed"].get("grounded") for c in reversed(oj) if c.get("parsed")), None)
                aq = rec.get("answer_quote_ok")
                if ng is True:
                    kind = "nano가 근거 있음이라 함"
                elif ng is False and aq is False:
                    kind = "nano는 근거 없음, 인용 검증 탈락"
                elif ng is False:
                    kind = "nano 근거 없음·인용 통과인데 놓침"
                else:
                    kind = "nano 판정 없음(호출 없음·파싱 실패·llm 기록 없음)"
                cls[kind] += 1
                miss.setdefault(i, []).append((tier_name(rec), str(ng), str(aq)))
            opp += g and not p["output"]["grounded"]
            if tier_name(rec).startswith("3"):
                gold = r.labels["unsupported"]
                pred = p["output"]["unsupported"]
                t3.setdefault(i, []).append(
                    (scoring.span_f1(pred, gold), len(N("".join(pred))), len(N("".join(gold))))
                )
    out(f"\n(2) 근거 판정 놓침(정답 근거 없음인데 예측 근거 있음) 문항 {len(miss)}개")
    out("id       놓침/실행  tier | nano grounded | answer_quote_ok")
    for i, m in sorted(miss.items(), key=lambda x: -len(x[1]))[:top]:
        out(
            f"{i[:8]:<9}{len(m):>3}/{nrun[i]:<6} {dist(Counter(x[0] for x in m))} | "
            f"{dist(Counter(x[1] for x in m))} | {dist(Counter(x[2] for x in m))}"
        )
    out("놓침 원인 요약(전체 실행 합): " + (", ".join(f"{k} {v}건" for k, v in cls.most_common()) or "없음"))
    out(f"(3) 반대 방향(정답 근거 있음인데 예측 근거 없음): {opp}건 / {len(runs)}회 = 회당 {opp / len(runs):.2f}")
    low = sorted(t3.items(), key=lambda x: mean(v[0] for v in x[1]))[:10]
    out(f"(4) tier 3 행의 구간 F1이 낮은 문항 하위 {len(low)}개(tier 3 문항 {len(t3)}개)")
    out("id       tier3 횟수  평균 F1  평균 예측 길이/정답 길이(정규화 문자)")
    for i, v in low:
        pl, gl = mean(x[1] for x in v), mean(x[2] for x in v)
        out(f"{i[:8]:<9}{len(v):>8}{mean(x[0] for x in v):9.3f}  {pl:.1f}/{gl:.1f}")


# ---------------- 자체 검사 ----------------
def check(ok: bool, msg: object = "") -> None:
    if not ok:
        raise SystemExit(f"실패: {msg!r}")


def selftest() -> None:
    a = "이것은 사과이고, 저것은 배입니다. 마지막 문장이에요"
    check([a[s:e] for s, e in clauses(a)] == ["이것은 사과이고,", "저것은 배입니다.", "마지막 문장이에요"], clauses(a))
    close = lambda x, y: abs(x - y) < 1e-9  # noqa: E731

    def run(text, gold):
        cl = clauses(text)
        G = locate(text, gold)
        return {k: oracle(text, u, G, gold) for k, u in
                {"문장": sr.sentences(text), "절": cl, "창": windows(text, cl)}.items()}  # fmt: skip

    # 절 2개, 정답 "라마바"는 앞 절 안: 절 = 6자 중 3자(P .5, R 1, F1 2/3), 문장 = 12자 중 3자(F1 .4)
    r = run("가나다 라마바, 사아자 차카타.", ["라마바"])
    check(close(r["절"][0][2], 2 / 3) and close(r["절"][1][2], 2 / 3) and close(r["문장"][0][2], 0.4), r)
    # 한 절 7어절: 창(4어절)이 절보다 정밀하다. 창 "오육칠" F1 .8, 절 4/9
    r = run("일 이 삼 사 오 육 칠", ["오 육"])
    check(close(r["창"][0][2], 0.8) and close(r["절"][0][2], 4 / 9), r)
    # 닿는 전부(6/13)보다 최적 부분집합(.5, "차"만)이 높다
    r = run("가나 다라마바사아자, 차", ["가나", "차"])
    check(close(r["절"][0][2], 6 / 13) and close(r["절"][1][2], 0.5), r)
    check(close(prf(["라마바"], ["라마바", "x"])[2], scoring.span_f1(["라마바"], ["라마바", "x"])))
    # 불일치 집계: 2차 사용·nano 없음 행은 뺀다
    v = lambda n, k, ll, g, p, s=False: {"nano": n, "knn": k, "lr": ll, "gold": g, "pred": p, "second": s}  # noqa: E731
    t = disagree_table(
        [v(True, False, False, True, False), v(True, False, True, True, True), v(False, True, True, False, True),
         v(False, True, True, False, False, True), v(None, True, True, True, True)], 1)  # fmt: skip
    want = {"(i) nano≠kNN": (1, 1, 1), "(ii) nano≠LR": (1, 1, 0), "(iii) kNN·LR 둘 다 반대": (1, 1, 0),
            "참고: 불일치 무관 전체": (1, 1, 1)}  # fmt: skip
    check(all((t[k]["miss"], t[k]["fp"], t[k]["ok"]) == w for k, w in want.items()), t)
    out("selftest ok")


def main() -> None:
    ap = argparse.ArgumentParser(description="0단계: 호출 0회 분석(a 절 오라클, d 불일치, f 호출 상태, items 문항표)")
    ap.add_argument("--runs", nargs="+", metavar="DIR")
    ap.add_argument("--section", choices=("a", "d", "f", "items", "all"), default="all")
    ap.add_argument("--config", default="configs/default.yaml")
    ap.add_argument("--config-name")
    ap.add_argument("--top", type=int, default=30, help="문항 표에 찍을 문항 수")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.runs:
        ap.error("--runs가 필요하다")
    ctx = pc.make_ctx(args.config)
    runs, notes = read_recs(args.runs, {r.id for r in ctx["rows"]}, args.config_name)
    for n in notes:
        out(n)
    pc.check(runs, f"쓸 수 있는 실행이 없다: {args.runs}")
    out(f"문항 {len(ctx['rows'])}개(홀드아웃 제외), 실행 {len(runs)}개: " + ", ".join(f"{k[0]}:{k[1]} r{k[2]}" for k in sorted(runs)))
    for s, f in (("a", lambda: sec_a(ctx, runs)), ("d", lambda: sec_d(ctx, runs)),
                 ("f", lambda: sec_f(ctx, runs)), ("items", lambda: sec_items(ctx, runs, args.top))):  # fmt: skip
        if args.section in (s, "all"):
            f()


if __name__ == "__main__":
    main()
