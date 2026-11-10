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
