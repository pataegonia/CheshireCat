"""리포트 통계. S1.1은 Wilson 구간만 둔다(묶음 부트스트랩은 S2.9, master-plan §3)."""

from scipy.stats import binomtest


def wilson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    ci = binomtest(k, n).proportion_ci(confidence_level=conf, method="wilson")
    return ci.low, ci.high
