"""리포트 통계 (PRD §7, R8 §B2).

Wilson·Clopper–Pearson 구간, 정확 McNemar, Holm, 주장에 필요한 표본 수.
"""

from scipy.stats import binom, binomtest


def wilson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    ci = binomtest(k, n).proportion_ci(confidence_level=conf, method="wilson")
    return ci.low, ci.high


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    ci = binomtest(k, n).proportion_ci(confidence_level=conf, method="exact")
    return ci.low, ci.high


def cp_upper_one_sided(k: int, n: int, conf: float = 0.95) -> float:
    """실패율의 단측 conf 상한. 양측 (2·conf − 1) 구간의 위쪽 끝과 같다."""
    return clopper_pearson(k, n, 2 * conf - 1)[1]


def mcnemar_exact(b: int, c: int) -> float:
    """불일치 쌍 b·c의 정확 McNemar 양측 p값."""
    return binomtest(b, b + c, 0.5).pvalue if b + c else 1.0


def holm(pvals: list[float]) -> list[float]:
    """Holm 보정 p값(원래 순서). 정렬 → (m − i)·p → 누적 최대 → 1로 자름."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvals[i])
        adjusted[i] = min(1.0, running)
    return adjusted


def min_n_for_claim(failures: int, max_rate: float = 0.05, conf: float = 0.95) -> int:
    """실패 failures건으로 "실패율 ≤ max_rate"를 단측 conf로 주장하는 최소 n."""
    n = failures + 1
    while binom.cdf(failures, n, max_rate) > 1 - conf:
        n += 1
    return n
