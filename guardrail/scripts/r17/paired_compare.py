"""S3.13 짝 비교: 기준선 실행 묶음과 후보 실행 묶음을 비교한다(API 0회).

같은 설정도 전체 경로 실행마다 가드 점수가 88.78–93.34로 흔들린다(9회, sd 약 1.4). 실행끼리는 독립이다.

- 1차 검정(채택 판단): 문항은 고정하고 실행 라벨만 섞는 순열 검정. 통계량 = 후보 실행 평균 − 기준 실행 평균.
  질문은 "이 개발 세트에서 후보가 실행 잡음을 넘어 나은가"다. 실행을 통째로 섞으므로 실행 안 문항 간 상관
  (같은 시각의 API 상태, 서로 few-shot인 짝 문항)이 있어도 유효하다. 가정은 하나, 기준·후보 실행이 같은 날
  번갈아 돌아 교환 가능하다는 것. 조합 수가 EXACT_MAX 이하면 정확 순열, 넘으면 몬테카를로.
- 2차 보고(참고, 판정 아님): 실행은 고정하고 군집만 복원추출한 95% 구간. "비공개 세트로 일반화되는가"의
  대략값이다. 실행 단위 공유 효과가 있으면 좁게 나오고, 몇 문항만 바뀐 후보에는 정보가 거의 없다.
- 옛 설계(실행과 군집을 함께 재추출)는 실행 안 문항이 독립이면 실행 잡음을 여러 번 센다(문항별 차이에 남은
  실행 잡음을 군집 재추출이 또 세고, 실행 재추출이 얹은 잡음까지 센다). 합성 귀무에서 sd 1.5–1.8배라
  오경보 0–1%였다. 실행 단위 공유 효과가 크면 반대로 좁아진다(sd 0.85배). 어느 쪽이든 명목 수준이 아니다.
- 사전 등록: 후보마다 1차 지표 하나(--primary, 겨냥 성분)와 총점 허용 하락폭(--guard-margin)을 실행 전에 적는다.
  후보 4–6개를 같은 기준선에 대고 고르면 Holm으로 α를 나눈다(가장 작은 p부터 α/m, α/(m−1), …).

실행(guardrail/에서):
`uv run python -X utf8 scripts/r17/paired_compare.py --base runs/A runs/B --cand runs/C runs/D --primary block_errors`
`uv run python -X utf8 scripts/r17/paired_compare.py --null-check runs/s43-base1 ... runs/s44-base3`
`uv run python -X utf8 scripts/r17/paired_compare.py --null-check runs/s4* --group-split s43 s44`
`uv run python -X utf8 scripts/r17/paired_compare.py --power runs/s43-base1 ... [--inject-p 0.1 0.2 0.5]`
`uv run python -X utf8 scripts/r17/paired_compare.py --selftest`

- 실행 하나 = (폴더, config, repeat). 정답은 기록에 없어 공개 세트에서 붙인다. guard 행 중 홀드아웃 군집이
  아닌 것만 쓴다. summary.json에 invalid가 있거나 입력·출력 단독 실행인 폴더는 뺀다.
- 채점은 실제 채점기 `score_guard`를 실행마다 문항마다 한 번만 부르고 그 결과(충분통계)를 배열로 둔다.
  가중치가 모두 1이면 `score_responses`와 같아야 하고 불러올 때마다 확인한다. --ids는 같은 식에 0/1 가중치를 준다.
- 문항 문구는 출력하지 않는다(id 앞 8자만).
"""

import argparse
import json
import sys
import tempfile
from itertools import combinations
from math import comb
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.stats import chi2, norm

from guardrail.eval.score import score_responses, scoring

METRICS = ("guard", "block_f1", "category", "output", "span", "grounded", "block_errors")
LOWER_BETTER = {"block_errors"}  # 나머지는 높을수록 좋다
RAW = ("guard", "block_errors")  # 점수 단위 변환(×100)을 하지 않는 지표
RUN_KEYS = ("tp", "fp", "fn", "cat_ok", "oc", "span", "safe", "valid", "pts")  # 실행×문항
NO_DIFF, BETTER, WORSE = "차이 없음", "후보 나음", "후보 나쁨"
TOL_GUARD = 0.005 + 1e-9  # 채점기는 guard_score만 소수 둘째 자리로 반올림한다
TOL = 1e-9
ALPHA = 0.05  # 양측. 방향마다 α/2
HIT = 0.8  # 검정력 목표
EXACT_MAX = 20000  # 라벨 조합 수가 이 이하면 정확 순열(모의 반복용)
EXACT_MAX_COMPARE = 200_000  # 실제 비교 한 번: C(20,10) = 184756까지 정확
TARGET = {"input": "block_errors", "span": "span", "grounded": "grounded"}  # 주입 성분 → 겨냥 지표
LOSS = {"input": "block", "span": "span", "grounded": "grounded"}  # 주입 성분 → wrong_of 키


def out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def check(ok: bool, msg: object = "") -> None:
    if not ok:  # python -O에서도 남게 assert 대신
        raise SystemExit(f"실패: {msg!r}")


def unit(m: str) -> float:
    return 1.0 if m in RAW else 100.0


# ---------------- 순수 계산(파일 없이 배열만) ----------------
def stats_from_results(runs: list[list[dict]]) -> dict:
    """실행마다 score_guard 결과 목록(문항 순서 같음) → 충분통계 배열. 실행×문항, 속성은 문항."""
    first = runs[0]
    S = {
        "gold_block": np.array([r["gold_block"] for r in first], float),
        "cat_sc": np.array([r["category_scored"] for r in first], float),
        "has_out": np.array([r["has_output"] for r in first], float),
    }
    for run in runs:  # 채점기가 정답에서만 정하는 값이라 실행마다 같아야 한다
        check(
            len(run) == len(first)
            and all(
                (r["gold_block"], r["category_scored"], r["has_output"])
                == (f["gold_block"], f["category_scored"], f["has_output"])
                for r, f in zip(run, first, strict=True)
            ),
            "문항 속성이 실행마다 다르다",
        )

    def col(f) -> np.ndarray:
        return np.array([[f(r) for r in run] for run in runs], float)

    ok = col(lambda r: r["input_correct"])  # 무효 응답은 False라 차단 행은 fn, 허용 행은 fp
    gb = S["gold_block"]
    S |= {
        "tp": gb * ok,
        "fn": gb * (1 - ok),
        "fp": (1 - gb) * (1 - ok),
        "cat_ok": col(lambda r: r["category_scored"] and r["category_correct"]),
        "oc": col(lambda r: bool(r["output_correct"])),
        "span": col(lambda r: r["span_f1"] or 0.0),
        "safe": col(lambda r: bool(r["safe_correct"])),
        "valid": col(lambda r: r["valid"]),
        "pts": col(scoring.guard_points),
    }
    return S


def take(S: dict, idx) -> dict:
    """고른 실행만(문항 속성은 그대로)."""
    return {k: (v[idx] if k in RUN_KEYS else v) for k, v in S.items()}


def guard_from_stats(S: dict, W) -> dict[str, np.ndarray]:
    """문항 가중치 W(B×N 또는 N)로 지표를 다시 계산한다. 값마다 실행×B 배열.

    채점기 공식 그대로이고 반올림만 없다: guard 0–100, 나머지 0–1, block_errors는 fp+fn 건수.
    """
    W = np.atleast_2d(np.asarray(W, float))
    ho, cs = S["has_out"], S["cat_sc"]

    def tot(X) -> np.ndarray:
        return X @ W.T

    tp, fp, fn = tot(S["tp"]), tot(S["fp"]), tot(S["fn"])
    den = 2 * tp + fp + fn
    f1 = np.where(den > 0, 2 * tp / np.where(den > 0, den, 1), 1.0)  # tp+fp+fn = 0이면 1.0
    n_c, n_o = cs @ W.T, ho @ W.T  # 범주 채점 행·출력 행의 가중 개수(B)

    def mean_over(X, n) -> np.ndarray:  # 해당 행이 없으면 0(채점기가 그 항을 빼는 경우)
        return np.where(n > 0, tot(X) / np.maximum(n, 1e-12), 0.0)

    cat = mean_over(S["cat_ok"], n_c)
    outp = mean_over(ho * (S["oc"] + S["span"] + S["safe"]) / 3, n_o)
    wd, wc = scoring.GUARD_DECISION_WEIGHT, scoring.GUARD_CATEGORY_WEIGHT
    w_c, w_o = wc * (n_c > 0), (1 - wd - wc) * (n_o > 0)  # 없는 항은 빼고 가중치를 다시 맞춘다
    return {
        "guard": 100 * (wd * f1 + w_c * cat + w_o * outp) / (wd + w_c + w_o),
        "block_f1": f1,
        "category": cat,
        "output": outp,
        "span": mean_over(ho * S["span"], n_o),
        "grounded": mean_over(ho * S["oc"], n_o),
        "block_errors": fp + fn,
    }


def point(S: dict, W=None) -> dict[str, np.ndarray]:
    """실행별 지표(채점기 단위). W = 문항 0/1 가중치(--ids), 없으면 전부 1."""
    W = np.ones(S["gold_block"].shape[0]) if W is None else W
    return {m: v[:, 0] for m, v in guard_from_stats(S, W).items()}


def labels(
    nb: int, nc: int, n_perm: int, rng, exact_max: int = EXACT_MAX
) -> tuple[np.ndarray, bool]:
    """순열 라벨(행 = 한 배치, 1 = 후보). 조합 수가 exact_max 이하면 전부(관측 배치 포함), 아니면 무작위."""
    n = nb + nc
    if comb(n, nc) <= exact_max:
        M = np.zeros((comb(n, nc), n))
        for i, c in enumerate(combinations(range(n), nc)):
            M[i, list(c)] = 1.0
        return M, True
    M = np.zeros((n_perm, n))
    M[:, nb:] = 1.0
    return rng.permuted(M, axis=1), False


def min_p(M: np.ndarray, exact: bool) -> float:
    """이 라벨 집합으로 낼 수 있는 가장 작은 양측 p. ALPHA 이상이면 어떤 차이도 유의할 수 없다."""
    return min(1.0, 2 / M.shape[0] if exact else 2 / (M.shape[0] + 1))


def perm_p(X, nb: int, M: np.ndarray, exact: bool) -> tuple[np.ndarray, np.ndarray]:
    """1차 검정. X = 행마다 [기준 실행 nb개, 후보 실행 nc개]의 실행별 지표. (차이, 양측 p) 행별.

    양측 p = 2·min(위쪽, 아래쪽 한쪽 p). 몬테카를로면 (1+k)/(1+B). 동률은 허용오차를 둔 ≥로 센다.
    """
    X = np.atleast_2d(np.asarray(X, float))
    nc = X.shape[1] - nb
    T = X @ M.T / nc - X @ (1 - M).T / nb
    t = X[:, nb:].mean(1) - X[:, :nb].mean(1)
    eps = (1e-9 * (1 + np.abs(X).max(1)))[:, None]
    up = (t[:, None] - eps <= T).sum(1)
    dn = (t[:, None] + eps >= T).sum(1)
    b = M.shape[0]
    up, dn = (up / b, dn / b) if exact else ((up + 1) / (b + 1), (dn + 1) / (b + 1))
    return t, np.minimum(1.0, 2 * np.minimum(up, dn))


def verdict(metric: str, r: dict) -> str:
    if r["p"] >= ALPHA:
        return NO_DIFF
    return BETTER if (r["diff"] > 0) != (metric in LOWER_BETTER) else WORSE


def cluster_boot(Sb: dict, Sc: dict, cl: np.ndarray, W0, n_boot: int, rng) -> dict:
    """2차(참고): 실행은 고정하고 군집만 복원추출. 문항 가중치 = 군집이 뽑힌 횟수 × W0. 지표별 95% 구간."""
    n_cl = int(cl.max()) + 1
    W = rng.multinomial(n_cl, np.full(n_cl, 1 / n_cl), size=n_boot)[:, cl] * W0
    gb, gc = guard_from_stats(Sb, W), guard_from_stats(Sc, W)
    return {
        m: np.percentile((gc[m].mean(0) - gb[m].mean(0)) * unit(m), [2.5, 97.5]) for m in METRICS
    }


def compare(Sb: dict, Sc: dict, cl, W0, n_perm: int, n_boot: int, rng) -> tuple[dict, float, str]:
    """지표별 {기준, 후보, 차이, p(1차 순열), lo/hi(2차 군집 구간)}, 최소 가능 p, 순열 종류."""
    nb, nc = Sb["tp"].shape[0], Sc["tp"].shape[0]
    M, exact = labels(nb, nc, n_perm, rng, EXACT_MAX_COMPARE)  # 한 번뿐이라 10 대 10까지 정확
    pb, pc = point(Sb, W0), point(Sc, W0)
    ci = cluster_boot(Sb, Sc, cl, W0, n_boot, rng)
    res = {}
    for m in METRICS:
        x = np.concatenate([pb[m], pc[m]]) * unit(m)
        d, p = perm_p(x, nb, M, exact)
        res[m] = {
            "base": float(x[:nb].mean()),
            "cand": float(x[nb:].mean()),
            "diff": float(d[0]),
            "p": float(p[0]),
            "lo": float(ci[m][0]),
            "hi": float(ci[m][1]),
        }
    kind = f"정확 {M.shape[0]}조합" if exact else f"몬테카를로 {M.shape[0]}회"
    return res, min_p(M, exact), kind


def wrong_of(S: dict, metric: str) -> np.ndarray:
    """문항 오답 점수(실행×문항, 클수록 나쁨). points = 1 − 행 guard_points."""
    ho = S["has_out"]
    return {
        "points": lambda: 1 - S["pts"],
        "block": lambda: S["fp"] + S["fn"],
        "category": lambda: S["cat_sc"] * (1 - S["cat_ok"]),
        "grounded": lambda: ho * (1 - S["oc"]),
        "span": lambda: ho * (1 - S["span"]),
    }[metric]()


def overdispersion(S: dict, W0) -> dict[str, float]:
    """φ = 실행별 오답 합의 분산 / 문항별 분산의 합. 실행 안 문항이 독립이면 1 근처, 실행 단위 공유
    효과(같은 시각 API 상태 등)가 있으면 1보다 크다. 문항별 층화 순열은 φ≈1일 때만 정당하다."""
    res = {}
    for key in ("block", "category", "grounded", "span", "points"):
        w = wrong_of(S, key) * W0
        vi = w.var(0, ddof=1).sum()
        res[key] = float(w.sum(1).var(ddof=1) / vi) if vi > TOL else float("nan")
    return res


def _halves(r: int, s: int, rng) -> tuple[np.ndarray, np.ndarray]:
    """실행을 무작위 반반. 홀수면 남는 하나를 번갈아(s가 짝수면 앞쪽) 준다."""
    perm = rng.permutation(r)
    nb = r // 2 + int(r % 2 == 1 and s % 2 == 0)
    return perm[:nb], perm[nb:]


def null_check(S: dict, W0, splits: int, n_perm: int, rng) -> dict[str, dict]:
    """같은 설정의 실행만으로 반반 비교를 splits번 해 1차 검정의 오경보율과 차이의 분포를 낸다.

    정확 순열이면 이 비율은 구성상 α 이하다(분할 전체가 곧 순열 분포). 코드 점검일 뿐 타당성 근거가 아니다.
    """
    r = S["tp"].shape[0]
    check(r >= 2, "귀무 점검은 실행이 2개 이상 필요하다")
    vals = {m: v * unit(m) for m, v in point(S, W0).items()}
    diffs = {m: [] for m in METRICS}
    alarm = dict.fromkeys(METRICS, 0)
    perms: dict[tuple, tuple] = {}
    for s in range(splits):
        b, c = _halves(r, s, rng)
        key = (len(b), len(c))
        if key not in perms:
            perms[key] = labels(*key, n_perm, rng)
        for m in METRICS:
            d, p = perm_p(np.concatenate([vals[m][b], vals[m][c]]), len(b), *perms[key])
            diffs[m].append(float(d[0]))
            alarm[m] += bool(p[0] < ALPHA)
    return {
        m: {
            "rate": alarm[m] / splits,
            "mean": float(np.mean(d)),
            "sd": float(np.std(d, ddof=1)) if splits > 1 else 0.0,
            "run_sd": float(vals[m].std(ddof=1)),
        }
        for m, d in diffs.items()
    }


def inject(S: dict, fix: np.ndarray, comp: str) -> dict:
    """가짜 후보. fix = 실행×문항 불리언(고칠 칸). input = 입력 판정(decision·범주)을 정답으로,
    span = 구간 F1을 1.0으로, grounded = 근거 판정을 정답으로(유효 응답만). pts·valid는 그대로 둔다."""
    T = dict(S)
    if comp == "input":
        T["tp"] = np.where(fix, S["gold_block"], S["tp"])
        T["fp"] = np.where(fix, 0.0, S["fp"])
        T["fn"] = np.where(fix, 0.0, S["fn"])
        T["cat_ok"] = np.where(fix, S["cat_sc"], S["cat_ok"])
    else:
        key = "span" if comp == "span" else "oc"
        T[key] = np.where(fix & (S["valid"] > 0), 1.0, S[key])
    return T


def power(
    S: dict, W0, comp: str, steps: list, mode: str, ns: list[int], sims: int, n_perm: int, rng
) -> tuple[list[dict], dict[int, float]]:
    """가상 실행으로 1차 검정의 검정력. 두 팔 모두 기록된 실행 r개에서 n개씩 복원추출하고 후보 팔에 효과를 넣는다.

    mode k: 겨냥 성분 오답률이 높은 문항 step개를 모든 후보 실행에서 고친다(기록 전체의 순서).
    mode p: 겨냥 성분에서 한 번이라도 틀린 문항을 후보 실행·문항마다 확률 step으로 고친다.
    단계·n 사이에 같은 난수(뽑은 실행, 균등 난수, 순열)를 쓴다. 단계 0 = 효과 없음(오경보율).
    """
    r, n_it = S["tp"].shape
    mets = ("guard", TARGET[comp])
    loss = wrong_of(S, LOSS[comp]).mean(0) * (1 if W0 is None else W0)  # --ids 밖 문항은 안 고친다
    order = [int(j) for j in np.lexsort((np.arange(n_it), -loss)) if loss[j] > TOL]
    nmax = max(ns)
    ib, ic = rng.integers(0, r, (2, sims, nmax))
    U = rng.random((sims, nmax, n_it)) if mode == "p" else None
    perms = {n: labels(n, n, n_perm, rng) for n in ns}
    p0 = point(S, W0)
    rows = []
    for step in (0, *steps):
        if mode == "k" and step > len(order):
            rows.append({"step": step, "effect": None})
            continue
        cand: dict[int, dict] = {}
        if mode == "k":
            fix = np.zeros((r, n_it), bool)
            fix[:, order[:step]] = True
            tv = point(inject(S, fix, comp), W0)
            eff = {m: float((tv[m] - p0[m]).mean() * unit(m)) for m in mets}
            cand = {n: {m: tv[m][ic[:, :n]] * unit(m) for m in mets} for n in ns}
        else:
            in_order = np.isin(np.arange(n_it), order)
            for n in ns:
                drawn = take(S, ic[:, :n].ravel())
                fix = (U[:, :n].reshape(-1, n_it) < step) & in_order
                tv = point(inject(drawn, fix, comp), W0)
                cand[n] = {m: tv[m].reshape(sims, n) * unit(m) for m in mets}
                if n == nmax:
                    pd = point(drawn, W0)
                    eff = {m: float((tv[m] - pd[m]).mean() * unit(m)) for m in mets}
        rate = {m: {} for m in mets}
        alarm = {m: {} for m in mets}
        for n in ns:
            for m in mets:
                X = np.concatenate([p0[m][ib[:, :n]] * unit(m), cand[n][m]], 1)
                d, p = perm_p(X, n, *perms[n])
                good = d < 0 if m in LOWER_BETTER else d > 0
                rate[m][n] = float(((p < ALPHA) & good).mean())
                alarm[m][n] = float((p < ALPHA).mean())
        rows.append({"step": step, "effect": eff, "rate": rate, "alarm": alarm, "items": order})
    return rows, {n: min_p(*perms[n]) for n in ns}


def mdd(rows: list[dict], m: str, n: int) -> float | None:
    """검정력 HIT에 처음 닿는 효과 크기(절댓값, 앞 단계와 선형 보간). 못 닿으면 None."""
    prev_e, prev_r = 0.0, rows[0]["rate"][m][n]
    for row in rows[1:]:
        if row["effect"] is None:
            continue
        e, rt = abs(row["effect"][m]), row["rate"][m][n]
        if rt >= HIT:
            return prev_e + (e - prev_e) * (HIT - prev_r) / (rt - prev_r)
        prev_e, prev_r = e, rt
    return None


# ---------------- 기록 읽기·채점 ----------------
def make_ctx(config: str) -> dict:
    """공개 세트(홀드아웃 군집 제외 guard 행, id 순)·분할·채점 입력. 공개 세트를 읽는 곳은 여기뿐이다."""
    from guardrail.config import load_config, resolve_path
    from guardrail.data import load_public
    from guardrail.eval import cv
    from guardrail.eval.run import _item

    cfg = load_config(resolve_path(config))
    split = cv.load_splits()
    held = set(split.holdout)
    rows = sorted(
        (r for r in load_public(cfg.paths.public) if r.endpoint == "guard"), key=lambda r: r.id
    )
    rows = [r for r in rows if split.cluster[r.id] not in held]
    names = sorted({split.cluster[r.id] for r in rows})
    pos = {n: i for i, n in enumerate(names)}
    return {
        "rows": rows,
        "items": [_item(r) for r in rows],
        "block": frozenset(cfg.block_categories),
        "cl": np.array([pos[split.cluster[r.id]] for r in rows]),
        "names": names,
        "gold": [f"{r.labels['decision']}/{r.labels['category']}" for r in rows],
    }


def subset_weights(ctx: dict, path: str | None) -> np.ndarray:
    """--ids 파일(한 줄에 id 하나, 실행 전에 적은 겨냥 문항) → 문항 0/1 가중치. 없으면 전부 1."""
    if not path:
        return np.ones(len(ctx["rows"]))
    want = {ln.strip() for ln in Path(path).read_text("utf-8").splitlines() if ln.strip()}
    have = {r.id for r in ctx["rows"]}
    check(want and want <= have, f"--ids: 개발 세트(홀드아웃 제외)에 없는 id {len(want - have)}개")
    out(f"--ids: 문항 {len(want)}개로 제한(모든 지표를 이 문항들로만 계산)")
    return np.array([float(r.id in want) for r in ctx["rows"]])


def read_runs(
    dirs: list[str], keep: set[str], config_name: str | None = None
) -> tuple[dict[tuple, dict[str, dict]], list[str]]:
    """{(폴더, config, repeat): {id: 응답}}과 안내 줄. invalid·입력/출력 단독 실행 폴더는 뺀다."""
    runs: dict[tuple, dict[str, dict]] = {}
    notes = []
    for d in dirs:
        p = Path(d)
        p = p.parent if p.is_file() else p
        summary = p / "summary.json"
        if summary.exists():
            s = json.loads(summary.read_text("utf-8"))
            if s.get("invalid"):
                notes.append(f"제외 {p.name}: invalid — {s['invalid']}")
                continue
            only = [k for k in ("input_only", "output_only") if s.get(k)]
            if only:
                notes.append(f"제외 {p.name}: {only[0]} 실행이라 가드 점수 비교에 쓰지 않는다")
                continue
        else:
            notes.append(
                f"경고 {p.name}: summary.json이 없어 invalid·단독 실행 여부를 확인하지 못했다"
            )
        path = p / "responses.jsonl"
        check(path.exists(), f"{path}가 없다")
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cfg = rec.get("config", "")
            if config_name is not None and cfg != config_name:
                continue
            if rec["id"] in keep and rec.get("response") is not None:
                runs.setdefault((p.name, cfg, rec["repeat"]), {})[rec["id"]] = rec["response"]
    return runs, notes


def make_bundle(
    dirs: list[str], ctx: dict, config_name: str | None = None
) -> tuple[list[tuple], dict, list[int]]:
    """(실행 이름들, 충분통계, 실행별 응답 누락 수). 불러올 때마다 채점기와 같은지 확인한다."""
    rows = ctx["rows"]
    runs, notes = read_runs(dirs, {r.id for r in rows}, config_name)
    for n in notes:
        out(n)
    labels_ = sorted(runs)
    if config_name is None and len({k[1] for k in labels_}) > 1:  # ablation 폴더는 설정 행이 섞인다
        out(
            f"경고: config가 여러 개 섞였다 {sorted({k[1] for k in labels_})}"
            " — 한 묶음으로 센다(--config-name으로 고른다)"
        )
    check(labels_, f"쓸 수 있는 실행이 없다: {dirs} (config-name={config_name})")
    miss = [sum(r.id not in runs[k] for r in rows) for k in labels_]
    results = [
        [
            scoring.score_guard(it, v, runs[k].get(r.id))
            for r, (it, v) in zip(rows, ctx["items"], strict=True)
        ]
        for k in labels_
    ]  # 응답이 없으면 None → 채점기가 무효로 센다
    S = stats_from_results(results)
    pts = point(S)
    for i, k in enumerate(labels_):
        want = score_responses(rows, runs[k], ctx["block"])
        o = want["output"]
        pairs = {
            "guard": want["guard_score"], "block_f1": want["block_f1"],
            "category": want["category_accuracy"], "output": o["mean"],
            "span": o["span_f1"], "grounded": o["grounded"],
        }  # fmt: skip
        for m, v in pairs.items():
            tol = TOL_GUARD if m == "guard" else TOL
            check(abs(pts[m][i] - v) <= tol, f"충분통계 ≠ score_responses: {k} {m} {pts[m][i]} {v}")
    return labels_, S, miss


def show_runs(name: str, labels_: list[tuple], S: dict, miss: list[int]) -> None:
    pts = point(S)
    out(f"{name}: 실행 {len(labels_)}개")
    for i, (folder, cfg, rep) in enumerate(labels_):
        gap = f", 응답 누락 {miss[i]}문항(무효로 채점)" if miss[i] else ""
        out(
            f"  {folder}:{cfg} r{rep}  guard {pts['guard'][i]:.2f}"
            f" 차단오답 {pts['block_errors'][i]:.0f}{gap}"
        )


# ---------------- 보고 ----------------
def print_diff(res: dict, primary: str, mp: float, kind: str, margin: float | None) -> None:
    out(
        f"\n1차: 실행 라벨 순열 검정({kind}), 양측 α={ALPHA}, 이 실행 수로 낼 수 있는 최소 p {mp:.4f}."
        "\n2차: 군집 재추출 95% 구간(실행 고정) — 일반화 참고용, 판정에 쓰지 않는다."
        "\nguard는 0–100, block_errors는 건수, 나머지는 ×100(점). * = 사전 등록 1차 지표"
    )
    if mp >= ALPHA:
        out(
            f"경고: 최소 p {mp:.3f} ≥ {ALPHA} — 이 실행 수로는 어떤 차이도 유의할 수 없다(팔당 5회 이상)"
        )
    out(f"{'지표':<14}{'기준':>8}{'후보':>8}{'차이':>8}{'p':>8}  {'판정':<8}{'2차 군집 구간':>22}")
    for m in METRICS:
        r = res[m]
        out(
            f"{('*' if m == primary else ' ') + m:<14}{r['base']:8.2f}{r['cand']:8.2f}"
            f"{r['diff']:+8.2f}{r['p']:8.4f}  {verdict(m, r):<8}  [{r['lo']:+8.2f},{r['hi']:+8.2f}]"
        )
    first = verdict(primary, res[primary])
    out(f"1차 판정({primary}): {first} (p {res[primary]['p']:.4f})")
    if margin is None:  # group-split: 같은 설정끼리라 채택 판단이 없다
        return
    g = res["guard"]
    guard_ok = g["diff"] >= -margin and verdict("guard", g) != WORSE
    out(
        f"총점 확인: guard 차이 {g['diff']:+.2f}, 허용 하락 {margin} → {'통과' if guard_ok else '실패'}"
        " (유의한 악화가 없고 점추정이 허용 안. 비열등 증명은 아니다)"
    )
    out(f"채택 근거: {'있음' if first == BETTER and guard_ok else '없음'}")


def print_items(Sb: dict, Sc: dict, ctx: dict, metric: str, top: int) -> None:
    """두 묶음에서 오답률이 다른 문항(차이 큰 순, 기술 통계). 문항 문구는 찍지 않는다."""
    wb, wc = wrong_of(Sb, metric), wrong_of(Sc, metric)
    d = wc.mean(0) - wb.mean(0)
    order = [int(j) for j in np.argsort(-np.abs(d), kind="stable") if abs(d[j]) > TOL]
    out(f"\n오답률이 다른 문항 {len(order)}개(기준 {metric}, 후보−기준 오답률 큰 순 {top}개)")
    if not order:
        return
    comp = ("block", "category", "grounded", "span")
    cb = {c: wrong_of(Sb, c).mean(0) for c in comp}
    cc = {c: wrong_of(Sc, c).mean(0) for c in comp}
    nb, nc = wb.shape[0], wc.shape[0]
    out("id       군집      정답           기준오답   후보오답    Δ차단  Δ범주  Δ근거  Δ구간")
    for j in order[:top]:
        out(
            f"{ctx['rows'][j].id[:8]:<9}{ctx['names'][ctx['cl'][j]][:8]:<10}{ctx['gold'][j]:<15}"
            f"{int((wb[:, j] > TOL).sum()):>4}/{nb:<5}{int((wc[:, j] > TOL).sum()):>4}/{nc:<5}"
            + "".join(f"{cc[c][j] - cb[c][j]:+7.2f}" for c in comp)
        )


def compare_report(
    ctx: dict, Sb: dict, Sc: dict, W0, args: argparse.Namespace, decide: bool = True
) -> None:
    rng = np.random.default_rng(args.seed)
    res, mp, kind = compare(Sb, Sc, ctx["cl"], W0, args.perms, args.n_boot or 2000, rng)
    print_diff(res, args.primary, mp, kind, args.guard_margin if decide else None)
    print_items(Sb, Sc, ctx, args.metric, args.top)


def print_null(nc: dict, phi: dict, splits: int, S: dict) -> None:
    r = S["tp"].shape[0]
    out(f"\n귀무 점검: 같은 설정 실행 {r}개를 무작위 반반 {splits}번, 1차 순열 검정")
    out("(정확 순열이면 오경보율은 구성상 α 이하다. 코드 점검일 뿐 타당성 근거가 아니다.")
    out(" 타당성은 실행 교환 가능성 — 날짜·묶음 효과는 --group-split로, 공유 효과는 아래 φ로 본다)")
    out(f"{'지표':<13}{'오경보율':>8}{'차이 평균':>10}{'차이 sd':>9}{'실행 sd':>9}")
    for m in METRICS:
        v = nc[m]
        out(f"{m:<13}{v['rate']:8.3f}{v['mean']:10.3f}{v['sd']:9.3f}{v['run_sd']:9.3f}")
    lo, hi = chi2.ppf([0.025, 0.975], r - 1) / (r - 1)
    out(
        f"\n과분산 φ(실행별 오답 합 분산 / 문항별 분산 합). 문항 독립이면 95% 범위 약 [{lo:.2f}, {hi:.2f}]"
    )
    out("  " + "  ".join(f"{k} {v:.2f}" for k, v in phi.items()))
    keys = ("block", "category", "grounded", "span")
    C = np.corrcoef([wrong_of(S, k).sum(1) for k in keys])
    out("성분 오답 합의 실행 간 상관(겨냥 밖 성분으로 분산을 줄일 여지. |r|이 작으면 없다):")
    for i, k in enumerate(keys):
        out(f"  {k:<9}" + "".join(f"{C[i, j]:+7.2f}" for j in range(len(keys))))


def print_power(rows: list[dict], mp: dict, comp: str, mode: str, S: dict, W0, sims: int) -> None:
    r = S["tp"].shape[0]
    tgt = TARGET[comp]
    mets = ("guard", tgt)
    ns = sorted(mp)
    out(
        f"\n검정력: 주입 {comp}({'문항 k개 고침' if mode == 'k' else '틀린 칸마다 확률 p로 고침'})"
        f", 겨냥 지표 {tgt}, 모의 {sims}회, 양측 α={ALPHA}"
    )
    sd_lo, sd_hi = np.sqrt((r - 1) / chi2.ppf([0.975, 0.025], r - 1))  # 정규 가정 sd 95% 구간 비
    out(
        f"가상 실행 = 기록된 실행 {r}개에서 팔마다 n개 복원추출(실제 기록이 {r}회뿐이라). 가정·한계:"
        f"\n  (1) 기록 {r}회가 실행 분포를 대표한다 — 참 sd는 기록 sd의 ×{sd_lo:.2f}–×{sd_hi:.2f}"
        "(95%)일 수 있고 MDD도 같은 비율로 흔들린다."
        f"\n  (2) 복원추출 가상 실행은 분산이 ({r}−1)/{r}로 줄고 같은 실행이 겹친다 — 검정력이 낙관적이다."
        "\n  (3) 효과는 주입한 형태(고친 칸 말고 변화 없음)로만 들어간다. 실제 후보는 다른 문항도 흔든다."
    )
    out(
        f"{'단계':>6}{'Δguard':>9}{'Δ' + tgt:>15} | "
        + "".join(f"{tgt[:5]} n={n:<3}" for n in ns)
        + "| "
        + "".join(f"guard n={n:<3}" for n in ns)
    )
    for row in rows:
        if row["effect"] is None:
            out(f"{row['step']:>6}  (효과를 줄 문항이 {len(rows[0]['items'])}개뿐이라 건너뜀)")
            continue
        key = "alarm" if row["step"] == 0 else "rate"  # 단계 0은 양방향 오경보율
        e = row["effect"]
        out(
            f"{row['step']:>6}{e['guard']:+9.3f}{e[tgt]:+15.3f} | "
            + "".join(f"{row[key][tgt][n]:>11.3f}" for n in ns)
            + "| "
            + "".join(f"{row[key]['guard'][n]:>11.3f}" for n in ns)
        )
    sd = {m: float((point(S, W0)[m] * unit(m)).std(ddof=1)) for m in mets}
    z = norm.ppf(1 - ALPHA / 2) + norm.ppf(HIT)
    out(f"\n{HIT:.0%} 검정력 최소 차이(MDD, 보간). 정규근사 = {z:.2f}·sd·√(2/n), 기록 sd 사용")
    out(
        f"{'n':>4}{'최소p':>8} | {'guard 모의':>11}{'정규근사':>9} | {tgt + ' 모의':>17}{'정규근사':>9}"
        f"   (sd: guard {sd['guard']:.3f}, {tgt} {sd[tgt]:.3f})"
    )
    for n in ns:
        cells = []
        for m in mets:
            v = mdd(rows, m, n)
            sim = "불가" if mp[n] >= ALPHA else (f"{v:.3f}" if v is not None else "범위 밖")
            cells.append(f"{sim:>{17 if m == tgt else 11}}{z * sd[m] * np.sqrt(2 / n):9.3f}")
        out(f"{n:>4}{mp[n]:8.3f} | {cells[0]} | {cells[1]}")
    out(
        "(불가 = 팔당 n회로는 순열 p가 α 아래로 못 내려간다. 범위 밖 = 시험한 효과로 80%에 못 닿음)"
    )


def run_modes(args: argparse.Namespace, ap: argparse.ArgumentParser) -> None:
    modes = [bool(args.base or args.cand), bool(args.null_check), bool(args.power)]
    if sum(modes) != 1:
        ap.error("--base/--cand, --null-check, --power 중 하나만 쓴다")
    if bool(args.base) != bool(args.cand):
        ap.error("--base와 --cand를 함께 쓴다")
    if args.group_split and not args.null_check:
        ap.error("--group-split은 --null-check와 함께 쓴다")
    ctx = make_ctx(args.config)
    out(f"문항 {len(ctx['rows'])}개(홀드아웃 군집 제외), 군집 {len(ctx['names'])}개")
    W0 = subset_weights(ctx, args.ids)
    rng = np.random.default_rng(args.seed)
    if args.base:
        lb, Sb, mb = make_bundle(args.base, ctx, args.config_name)
        lc, Sc, mc = make_bundle(args.cand, ctx, args.config_name)
        show_runs("기준", lb, Sb, mb)
        show_runs("후보", lc, Sc, mc)
        compare_report(ctx, Sb, Sc, W0, args)
        return
    dirs = args.null_check or args.power
    labels_, S, miss = make_bundle(dirs, ctx, args.config_name)
    show_runs("기준선", labels_, S, miss)
    if args.power:
        mode, steps = ("p", args.inject_p) if args.inject_p else ("k", sorted(args.k))
        comps = list(TARGET) if args.power_component == "all" else [args.power_component]
        for comp in comps:
            rows, mp = power(S, W0, comp, steps, mode, args.n, args.sims, args.perms, rng)
            print_power(rows, mp, comp, mode, S, W0, args.sims)
    elif args.group_split:
        a, b = args.group_split
        ia = [i for i, k in enumerate(labels_) if k[0].startswith(a)]
        ib = [i for i, k in enumerate(labels_) if k[0].startswith(b)]
        check(
            ia and ib and not set(ia) & set(ib),
            f"접두 {a!r}, {b!r}로 나눈 실행: {len(ia)}, {len(ib)}개",
        )
        out(f"\n묶음 비교(날짜·묶음 효과, 1차 검정): {a}* {len(ia)}개 대 {b}* {len(ib)}개")
        out(
            "(같은 설정이니 '차이 없음'이어야 한다. 유의하면 실행이 교환 가능하지 않다 — 같은 날 번갈아 돌린다)"
        )
        compare_report(ctx, take(S, ia), take(S, ib), W0, args, decide=False)
    else:
        splits = args.splits or 300
        nc = null_check(S, W0, splits, args.perms, rng)
        print_null(nc, overdispersion(S, W0), splits, S)


# ---------------- 자체 검사(데이터·API 없이) ----------------
def _row(gold, cat_sc, has_out, w_in=False, w_cat=False, w_g=False, w_sp=False, w_safe=False):
    """score_guard 결과와 같은 키의 합성 행. w_* = 그 항목을 틀림."""
    return {
        "valid": True, "input_correct": not w_in, "category_correct": not w_cat,
        "category_scored": cat_sc, "false_block": False, "missed_block": False,
        "gold_block": gold, "has_output": has_out,
        "output_correct": (not w_g) if has_out else None,
        "span_f1": (0.5 if w_sp else 1.0) if has_out else None,
        "safe_correct": (not w_safe) if has_out else None,
    }  # fmt: skip


def _synth_z(n_runs, n, p, rng, shock=0.0, wrong=()):
    """합성 기록. 문항 속성은 고정(시드 7), 실행·문항·항목(입력·범주·근거·구간·safe)마다 독립으로 틀린다.
    오답 확률은 실행마다 p·exp(shock·z_실행) — shock > 0이면 실행 단위 공유 효과. wrong 문항은 입력을 늘 틀린다."""
    a = np.random.default_rng(7)
    gold, has_out = a.random(n) < 0.5, a.random(n) < 0.5
    cat_sc = gold | (a.random(n) < 0.3)
    pr = np.clip(p * np.exp(shock * rng.standard_normal((n_runs, 1, 1))), 0, 1)
    z = rng.random((n_runs, n, 5)) < pr
    z[:, list(wrong), 0] = True
    return gold, cat_sc, has_out, z


def _synth_rows(gold, cat_sc, has_out, z) -> list[list[dict]]:
    return [
        [_row(gold[j], cat_sc[j], has_out[j], *map(bool, z[i, j])) for j in range(len(gold))]
        for i in range(z.shape[0])
    ]


def _synth_S(gold, cat_sc, has_out, z) -> dict:
    """_synth_rows → stats_from_results와 같은 배열을 바로(빠르게) 만든다."""
    gb, cs, ho = gold.astype(float), cat_sc.astype(float), has_out.astype(float)
    ok, c_ok, g_ok, sp, sf = (
        1.0 - z[..., 0],
        1.0 - z[..., 1],
        1.0 - z[..., 2],
        z[..., 3],
        1.0 - z[..., 4],
    )
    span = ho * np.where(sp, 0.5, 1.0)
    return {
        "gold_block": gb, "cat_sc": cs, "has_out": ho,
        "tp": gb * ok, "fn": gb * (1 - ok), "fp": (1 - gb) * (1 - ok),
        "cat_ok": cs * c_ok, "oc": ho * g_ok, "span": span, "safe": ho * sf,
        "valid": np.ones(z.shape[:2]),
        "pts": (ok + cs * c_ok + ho * (g_ok + span + sf)) / (1 + cs + 3 * ho),
    }  # fmt: skip


def _selftest_scoring() -> None:
    # 충분통계 점수 = 손 계산. 행: tp / fn / fp / 평범한 allow
    r1 = _row(True, True, True)
    r2 = _row(True, True, False, w_in=True, w_cat=True)
    r3 = _row(False, False, True, w_in=True, w_g=True, w_sp=True)
    r4 = _row(False, False, False)
    S = stats_from_results([[r1, r2, r3, r4]])
    g = guard_from_stats(S, np.ones(4))
    # block F1 = 2/(2+1+1) = 0.5, 범주 = 1/2, 출력 = (1 + 0.5)/2 → 0.45*.5 + .1*.5 + .45*.75
    check(abs(g["guard"][0, 0] - 61.25) < 1e-9, g["guard"])
    check(abs(g["block_f1"][0, 0] - 0.5) < 1e-9 and g["block_errors"][0, 0] == 2, g)
    check(abs(g["output"][0, 0] - 0.75) < 1e-9 and abs(g["span"][0, 0] - 0.75) < 1e-9, g)
    check(abs(g["grounded"][0, 0] - 0.5) < 1e-9 and abs(g["category"][0, 0] - 0.5) < 1e-9, g)
    # 가중치 [2,1,1,1] = r1을 두 번: F1 4/6, 범주 2/3, 출력 2.5/3 → 74.1667, 채점기와도 같다
    w = np.array([2.0, 1, 1, 1])
    check(abs(guard_from_stats(S, w)["guard"][0, 0] - 74.16666666666667) < 1e-9)
    check(abs(scoring.guard_score([r1, r1, r2, r3, r4]) - 74.17) < 1e-9)
    # 퇴화: 범주·출력 항목이 없으면 그 항을 빼고 가중치를 다시 맞춘다. tp+fp+fn=0이면 F1 = 1
    S2 = stats_from_results([[_row(True, False, False), _row(False, False, False, w_in=True)]])
    check(abs(guard_from_stats(S2, np.ones(2))["guard"][0, 0] - 100 * 2 / 3) < 1e-9)
    S3 = stats_from_results([[_row(False, False, False)]])
    h = guard_from_stats(S3, np.ones(1))
    check(h["guard"][0, 0] == 100.0 and h["category"][0, 0] == 0.0 and h["output"][0, 0] == 0.0, h)
    # 무작위 합성: 정수 가중치 = 행 복제 = 채점기, 빠른 생성기 = stats_from_results
    syn = _synth_z(3, 50, 0.2, np.random.default_rng(1))
    runs = _synth_rows(*syn)
    S = stats_from_results(runs)
    fast = _synth_S(*syn)
    check(all(np.allclose(S[k], fast[k]) for k in S), "빠른 합성 ≠ stats_from_results")
    wt = np.random.default_rng(3).integers(0, 4, 50)
    for i, run in enumerate(runs):
        dup = [r for r, k in zip(run, wt, strict=True) for _ in range(k)]
        got = guard_from_stats(take(S, [i]), wt.astype(float))
        check(abs(got["guard"][0, 0] - scoring.guard_score(dup)) <= TOL_GUARD, (i, got["guard"]))
        check(abs(got["block_f1"][0, 0] - scoring.block_f1(dup)) < TOL, i)
        one = guard_from_stats(take(S, [i]), np.ones(50))["guard"][0, 0]
        check(abs(one - scoring.guard_score(run)) <= TOL_GUARD, (i, one))


def _selftest_perm() -> None:
    rng = np.random.default_rng(13)
    # 정확 순열: 3 대 3이면 20조합, 완전 분리여도 양측 p = 0.1 → 5%에서 가를 수 없다
    M, exact = labels(3, 3, 100, rng)
    check(exact and M.shape == (20, 6) and abs(min_p(M, exact) - 0.1) < 1e-12)
    d, p = perm_p([0, 0, 0, 1, 1, 1], 3, M, exact)
    check(d[0] == 1 and abs(p[0] - 0.1) < 1e-12, (d, p))
    M4, e4 = labels(4, 4, 100, rng)
    _, p = perm_p([5, 5, 6, 6, 1, 2, 2, 3], 4, M4, e4)  # 후보가 모두 낮음 → 2/70
    check(abs(p[0] - 2 / 70) < 1e-12, p)
    _, p = perm_p([1.0] * 8, 4, M4, e4)
    check(p[0] == 1.0, p)
    # 몬테카를로: C(24,12) > EXACT_MAX → (1+k)/(1+B)
    Mm, em = labels(12, 12, 999, rng)
    check(not em and Mm.shape == (999, 24) and (Mm.sum(1) == 12).all())
    _, p = perm_p([0.0] * 12 + [1.0] * 12, 12, Mm, em)
    check(abs(p[0] - 2 / 1000) < 1e-12, p)
    # 같은 두 묶음 → 차이 0, p 1, 군집 구간 [0, 0]
    syn = _synth_z(4, 80, 0.05, np.random.default_rng(11), wrong=(0, 2, 4, 6, 8, 10))
    base = _synth_S(*syn)
    cl = np.arange(80) // 2
    res, _, _ = compare(base, base, cl, np.ones(80), 999, 300, rng)
    check(all(res[m]["diff"] == 0 and res[m]["p"] == 1 and res[m]["lo"] == res[m]["hi"] == 0
              for m in METRICS), res)  # fmt: skip
    # 군집 6개를 전 실행에서 고친 후보: 1차 block_errors 후보 나음(4 대 4라 p = 2/70)
    fixed = _synth_S(*_synth_z(4, 80, 0.05, np.random.default_rng(12)))
    res, mp, _ = compare(base, fixed, cl, np.ones(80), 999, 1000, rng)
    check(verdict("block_errors", res["block_errors"]) == BETTER, res["block_errors"])
    check(res["block_errors"]["lo"] < 0 and abs(mp - 2 / 70) < 1e-12, res["block_errors"])


def _selftest_null() -> None:
    """합성 귀무(실행 잡음만, 실행을 매번 새로 생성)에서 1차 검정 오경보율이 명목 근처인지. 실행 단위 공유
    효과(shock)가 있어도 실행 통째 순열은 유효해야 한다. φ 진단이 공유 효과를 잡는지도 본다."""
    reps, n, items = 400, 6, 120
    M, exact = labels(n, n, 999, None)
    for shock in (0.0, 0.6):
        S = _synth_S(*_synth_z(reps * 2 * n, items, 0.12, np.random.default_rng(31), shock))
        vals = point(S)
        for m in ("guard", "block_errors", "span", "grounded", "category"):
            _, p = perm_p(vals[m].reshape(reps, 2 * n), n, M, exact)
            rate = float((p < ALPHA).mean())
            check(0.02 <= rate <= 0.09, (shock, m, rate))
    flat = _synth_S(*_synth_z(300, items, 0.12, np.random.default_rng(32)))
    shaky = _synth_S(*_synth_z(300, items, 0.12, np.random.default_rng(33), 0.6))
    one = np.ones(items)
    check(0.7 < overdispersion(flat, one)["block"] < 1.4, overdispersion(flat, one))
    check(overdispersion(shaky, one)["block"] > 2, overdispersion(shaky, one))


def _selftest_power() -> None:
    """검정력은 효과(k, p)와 n에 따라 단조 증가하고, 팔당 3회는 5%에서 불가다."""
    S = _synth_S(*_synth_z(9, 120, 0.04, np.random.default_rng(41), wrong=tuple(range(0, 24, 2))))
    ns = [3, 6, 12]
    rows, mp = power(S, None, "input", [1, 2, 4, 8], "k", ns, 200, 999, np.random.default_rng(5))
    check(mp[3] >= ALPHA and mp[6] < ALPHA, mp)
    rate = np.array([[row["rate"]["block_errors"][n] for n in ns] for row in rows])
    check((rate[:, 0] == 0).all(), rate)  # n=3
    check((np.diff(rate, axis=0) >= -0.02).all() and (np.diff(rate, axis=1) >= -0.03).all(), rate)
    check(rate[-1, -1] >= HIT and rate[0].max() <= 0.06, rate)
    check(all(rows[i + 1]["effect"]["guard"] > rows[i]["effect"]["guard"] for i in range(4)), rows)
    check(mdd(rows, "block_errors", 12) is not None, rate)
    w0 = (np.arange(120) >= 10).astype(float)  # --ids: 0–9번은 빼면 주입도 그 밖에서만
    rows, _ = power(S, w0, "input", [2], "k", [6], 20, 999, np.random.default_rng(5))
    check(min(rows[-1]["items"]) >= 10 and rows[-1]["effect"]["block_errors"] < 0, rows[-1])
    rows, _ = power(
        S, None, "span", [0.1, 0.3, 1.0], "p", [6, 12], 200, 999, np.random.default_rng(5)
    )
    rate = np.array([[row["rate"]["span"][n] for n in (6, 12)] for row in rows])
    eff = [row["effect"]["span"] for row in rows]
    check(eff[0] == 0 and all(eff[i + 1] > eff[i] for i in range(3)), eff)
    check((np.diff(rate, axis=0) >= -0.02).all() and rate[-1, -1] >= HIT, rate)


def _selftest_read() -> None:
    """입력·출력 단독·invalid 폴더는 빠지고, config가 섞인 폴더는 --config-name으로 고른다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rec = [
            {"config": c, "repeat": r, "id": i, "response": {"input": {}}}
            for c in ("default", "alt") for r in (0, 1) for i in ("x1", "x2", "zz")
        ]  # fmt: skip
        lines = "\n".join(json.dumps(x) for x in rec)
        for name, summ in (("io", {"input_only": True}), ("oo", {"output_only": True}),
                           ("bad", {"invalid": "429"}), ("ok", {"input_only": False})):  # fmt: skip
            (root / name).mkdir()
            (root / name / "summary.json").write_text(json.dumps(summ), "utf-8")
            (root / name / "responses.jsonl").write_text(lines, "utf-8")
        dirs = [str(root / d) for d in ("io", "oo", "bad", "ok")]
        runs, notes = read_runs(dirs, {"x1", "x2"})
        check(sorted(runs) == [("ok", c, r) for c in ("alt", "default") for r in (0, 1)], runs)
        check(sum(n.startswith("제외") for n in notes) == 3 and len(runs[("ok", "alt", 0)]) == 2)
        runs, _ = read_runs(dirs, {"x1", "x2"}, "default")
        check(sorted(runs) == [("ok", "default", 0), ("ok", "default", 1)], runs)
        (root / "ids.txt").write_text("x2\n\n", "utf-8")
        ctx = {"rows": [SimpleNamespace(id="x1"), SimpleNamespace(id="x2")]}
        check(subset_weights(ctx, str(root / "ids.txt")).tolist() == [0.0, 1.0])


def selftest() -> None:
    _selftest_scoring()
    _selftest_perm()
    _selftest_null()
    _selftest_power()
    _selftest_read()
    out("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="기준선·후보 실행 묶음 비교(실행 라벨 순열, 호출 0회)")
    ap.add_argument("--base", nargs="+", metavar="DIR", help="기준선 runs 폴더들")
    ap.add_argument("--cand", nargs="+", metavar="DIR", help="후보 runs 폴더들")
    ap.add_argument("--null-check", nargs="+", metavar="DIR", help="기준선만으로 오경보율·φ 점검")
    ap.add_argument("--group-split", nargs=2, metavar=("PREFIX_A", "PREFIX_B"),
                    help="--null-check와 함께: 폴더 이름 접두로 두 묶음을 나눠 1차 검정")  # fmt: skip
    ap.add_argument(
        "--power", nargs="+", metavar="DIR", help="기준선에 가짜 효과를 넣어 검정력·MDD"
    )
    ap.add_argument("--primary", choices=METRICS, default="guard",
                    help="사전 등록 1차 지표(후보가 겨냥한 성분, 실행 전에 정한다)")  # fmt: skip
    ap.add_argument("--guard-margin", type=float, default=0.5, help="총점 허용 하락폭(점)")
    ap.add_argument(
        "--ids", help="사전 등록 겨냥 문항 id 파일(한 줄 하나). 모든 지표를 이 문항으로만"
    )
    ap.add_argument(
        "--n", nargs="+", type=int, default=[3, 6, 9, 12, 20], help="--power 팔당 실행 수"
    )
    ap.add_argument("--k", nargs="+", type=int, default=[1, 2, 3, 4, 6, 8, 12, 16],
                    help="--power 고칠 문항 수(오답률 높은 순)")  # fmt: skip
    ap.add_argument("--inject-p", nargs="+", type=float,
                    help="--power 확률 주입: 틀린 칸마다 이 확률로 고친다(주면 --k 대신)")  # fmt: skip
    ap.add_argument(
        "--power-component", choices=("input", "span", "grounded", "all"), default="all"
    )
    ap.add_argument("--sims", type=int, default=200, help="--power 모의 횟수")
    ap.add_argument(
        "--perms", type=int, default=2000, help="몬테카를로 순열 횟수(정확 순열이 안 될 때)"
    )
    ap.add_argument("--splits", type=int, help="--null-check 반반 나누기 횟수(기본 300)")
    ap.add_argument("--n-boot", type=int, help="2차 군집 재추출 횟수(기본 2000)")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument(
        "--metric", choices=("points", "block", "category", "grounded", "span"), default="points",
        help="문항 표의 오답 기준(기본 points = 1 − 행 guard_points)",
    )  # fmt: skip
    ap.add_argument("--top", type=int, default=15, help="문항 표에 찍을 문항 수")
    ap.add_argument("--config", default="configs/default.yaml")
    ap.add_argument("--config-name", help="한 폴더에 config가 여러 개일 때 쓸 config 이름")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        run_modes(args, ap)
