"""Wilson 구간 기준값 (R8 §B2 ④, 10/30 검산)."""

import pytest

from guardrail.eval.stats import wilson


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [(4, 200, 0.008, 0.050), (10, 500, 0.011, 0.036), (15, 300, 0.031, 0.081)],
)
def test_wilson_reference_values(k, n, low, high):
    lo, hi = wilson(k, n)
    assert lo == pytest.approx(low, abs=0.0006) and hi == pytest.approx(high, abs=0.0006)


def test_cluster_bootstrap_resamples_clusters_not_rows():
    from guardrail.eval.stats import cluster_bootstrap_ci

    # 묶음 A(50행, 값 1)·B(50행, 값 0). 행 단위로 뽑으면 구간이 0.5 근처로 좁지만 묶음 단위는 [0,1]
    values = {f"a{i}": 1.0 for i in range(50)} | {f"b{i}": 0.0 for i in range(50)}
    cluster = {k: k[0] for k in values}
    lo, hi = cluster_bootstrap_ci(values, cluster, n_boot=500, seed=1)
    assert (lo, hi) == (0.0, 1.0)


def test_cluster_bootstrap_is_seeded_and_rejects_empty():
    from guardrail.eval.stats import cluster_bootstrap_ci

    values = {"x1": 0.2, "x2": 0.4, "y1": 0.9, "z1": 0.1}
    cluster = {"x1": "x", "x2": "x", "y1": "y", "z1": "z"}
    assert cluster_bootstrap_ci(values, cluster, seed=3) == cluster_bootstrap_ci(
        values, cluster, seed=3
    )
    with pytest.raises(ValueError):
        cluster_bootstrap_ci({}, {})
