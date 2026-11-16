"""리포트 통계: Wilson 구간(S1.1)과 묶음 단위 부트스트랩(S2.9, master-plan §3)."""

import random
from collections.abc import Mapping

from scipy.stats import binomtest


def wilson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    ci = binomtest(k, n).proportion_ci(confidence_level=conf, method="wilson")
    return ci.low, ci.high


def cluster_bootstrap_ci(
    values: Mapping[str, float],
    cluster: Mapping[str, str],
    *,
    n_boot: int = 1000,
    seed: int = 13,
    conf: float = 0.95,
) -> tuple[float, float]:
    """행 값의 평균에 대한 묶음 단위 부트스트랩 구간(ADR-0013). 짝 문항이 한 묶음이라 묶음을 뽑는다.

    설정 비교는 행별 차이(a - b)를 values로 넘긴다. 같은 시드면 같은 구간이다.
    """
    groups: dict[str, list[float]] = {}
    for rid, v in values.items():
        groups.setdefault(cluster[rid], []).append(v)
    if not groups:
        raise ValueError("값이 없다")
    sums = [(sum(g), len(g)) for g in groups.values()]
    rng = random.Random(seed)  # noqa: S311  재현용 시드
    means = []
    for _ in range(n_boot):
        pick = rng.choices(sums, k=len(sums))
        means.append(sum(s for s, _ in pick) / sum(n for _, n in pick))
    means.sort()
    tail = (1 - conf) / 2
    return means[int(tail * n_boot)], means[min(n_boot - 1, int((1 - tail) * n_boot))]
