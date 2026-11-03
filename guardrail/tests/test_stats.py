"""S0.7 통계 기준값 (PRD §7, R8 §B2 ④, 10/30 검산)."""

import pytest

from guardrail.eval.stats import (
    clopper_pearson,
    cp_upper_one_sided,
    holm,
    mcnemar_exact,
    min_n_for_claim,
    wilson,
)


def test_min_n_for_claim_matches_prd():
    assert [min_n_for_claim(f) for f in range(6)] == [59, 93, 124, 153, 181, 208]


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [(4, 200, 0.008, 0.050), (10, 500, 0.011, 0.036), (15, 300, 0.031, 0.081)],
)
def test_wilson_reference_values(k, n, low, high):
    lo, hi = wilson(k, n)
    assert lo == pytest.approx(low, abs=0.0006) and hi == pytest.approx(high, abs=0.0006)


def test_cp_upper_one_sided_59_rule():
    assert cp_upper_one_sided(0, 59) <= 0.05
    assert cp_upper_one_sided(0, 58) > 0.05


def test_clopper_pearson_contains_point_estimate():
    lo, hi = clopper_pearson(3, 50)
    assert lo < 0.06 < hi


def test_mcnemar_and_holm_reference_values():
    assert mcnemar_exact(10, 0) == pytest.approx(0.001953125)
    assert mcnemar_exact(0, 0) == 1.0
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])
