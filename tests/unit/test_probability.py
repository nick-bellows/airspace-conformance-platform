"""The conflict-probability math, checked against a reference implementation.

`acp.services.conformance.probability` computes a non-central chi-squared CDF by
hand, because the conformance service runs with numpy only -- scipy arrives with
scikit-learn, and the `degradation` CI job proves the service works with the ml
extra genuinely uninstalled.

Hand-rolling a special function is worth doing only if it is checked against
something that did not come from the same head. scipy is available in the dev
environment and is used here purely as an oracle; nothing imports it at runtime,
which `test_architecture.py` enforces.
"""

from __future__ import annotations

import math

import pytest

from acp.services.conformance.probability import (
    SIGMA_CUTOFF,
    band_probability,
    conflict_probability,
    disc_probability,
    projected_sigma,
)

scipy_stats = pytest.importorskip("scipy.stats", reason="oracle only; not a runtime dependency")


# The grid deliberately spans four orders of magnitude of sigma and crosses the
# 5 NM standard from both sides, because the interesting failures are at the
# boundary and in the tails where the series could underflow.
@pytest.mark.parametrize("sigma_nm", [0.01, 0.05, 0.2, 1.0, 3.0, 10.0, 40.0])
@pytest.mark.parametrize("miss_nm", [0.0, 0.5, 2.0, 4.9, 5.0, 5.1, 8.0, 25.0, 100.0])
def test_disc_probability_matches_scipy(sigma_nm: float, miss_nm: float) -> None:
    radius_nm = 5.0
    mine = disc_probability(miss_nm, radius_nm, sigma_nm)
    reference = float(
        scipy_stats.ncx2.cdf((radius_nm / sigma_nm) ** 2, df=2, nc=(miss_nm / sigma_nm) ** 2)
    )
    assert mine == pytest.approx(reference, abs=1e-9)


@pytest.mark.parametrize("sigma_ft", [1.0, 50.0, 250.0, 2000.0])
@pytest.mark.parametrize("offset_ft", [0.0, 400.0, 999.0, 1001.0, 5000.0])
def test_band_probability_matches_scipy(sigma_ft: float, offset_ft: float) -> None:
    half_width = 1000.0
    mine = band_probability(offset_ft, half_width, sigma_ft)
    reference = float(
        scipy_stats.norm.cdf((half_width - offset_ft) / sigma_ft)
        - scipy_stats.norm.cdf((-half_width - offset_ft) / sigma_ft)
    )
    assert mine == pytest.approx(reference, abs=1e-12)


def test_a_dead_centre_prediction_with_tight_covariance_is_near_certain() -> None:
    assert disc_probability(0.0, 5.0, 0.1) > 0.999999


def test_a_far_miss_with_tight_covariance_is_effectively_impossible() -> None:
    assert disc_probability(50.0, 5.0, 0.1) == 0.0


def test_a_far_miss_with_huge_covariance_is_not_dismissed() -> None:
    """The whole point: distance alone does not settle it.

    A 20 NM predicted miss is nowhere near the 5 NM standard, but if the tracks
    are only known to +/- 15 NM at closest approach then a breach is a real
    possibility and the deterministic detector's confident "no" is wrong.
    """
    assert disc_probability(20.0, 5.0, 15.0) > 0.01


def test_a_near_miss_with_huge_covariance_is_not_confident() -> None:
    """And the converse, which is where the precision of 0.57 comes from.

    A predicted 4.9 NM miss sits just inside the standard and the deterministic
    detector alerts at full confidence. With 8 NM of uncertainty the honest
    answer is that it is closer to a coin toss than to a certainty.
    """
    assert disc_probability(4.9, 5.0, 8.0) < 0.25


def test_zero_sigma_degrades_to_the_deterministic_test() -> None:
    """A filter claiming perfect knowledge must not divide by zero."""
    assert disc_probability(4.9, 5.0, 0.0) == 1.0
    assert disc_probability(5.1, 5.0, 0.0) == 0.0
    assert band_probability(999.0, 1000.0, 0.0) == 1.0
    assert band_probability(1001.0, 1000.0, 0.0) == 0.0


def test_probability_is_monotone_in_the_predicted_miss() -> None:
    """Moving the prediction further away can never make a breach likelier."""
    previous = 1.1
    for miss_nm in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0]:
        current = disc_probability(miss_nm, 5.0, 2.0)
        assert current <= previous
        previous = current


def test_probability_is_monotone_in_uncertainty_for_a_miss_inside_the_standard() -> None:
    """A prediction already inside the standard only gets less certain."""
    previous = 1.1
    for sigma_nm in [0.01, 0.1, 0.5, 1.0, 2.0, 5.0, 20.0]:
        current = disc_probability(1.0, 5.0, sigma_nm)
        assert current <= previous
        previous = current


def test_projected_sigma_grows_with_the_lookahead() -> None:
    """Velocity error integrates, which is why a distant CPA is a weak claim."""
    near = projected_sigma(0.1, 0.1, 0.001, 0.001, 30.0)
    far = projected_sigma(0.1, 0.1, 0.001, 0.001, 300.0)
    assert far > near


def test_the_velocity_term_dominates_at_a_long_lookahead() -> None:
    """With position known perfectly, uncertainty is linear in the lookahead.

    This is the term the deterministic detector throws away, and the reason a
    conflict predicted five minutes out deserves less confidence than the same
    geometry thirty seconds out.
    """
    at_30 = projected_sigma(0.0, 0.0, 0.001, 0.001, 30.0)
    at_300 = projected_sigma(0.0, 0.0, 0.001, 0.001, 300.0)
    assert at_300 == pytest.approx(at_30 * 10.0)


def test_projected_sigma_adds_variances_not_deviations() -> None:
    """Two tracks each +/- 0.3 NM are +/- 0.42 NM apart, not +/- 0.6."""
    assert projected_sigma(0.3, 0.3, 0.0, 0.0, 0.0) == pytest.approx(0.3 * math.sqrt(2.0))


def test_a_conflict_needs_both_standards() -> None:
    """Certain laterally, impossible vertically, so the product is negligible."""
    probability = conflict_probability(
        min_horizontal_nm=0.0,
        min_vertical_ft=40000.0,
        horizontal_sigma_nm=0.1,
        vertical_sigma_ft=50.0,
        horizontal_standard_nm=5.0,
        vertical_standard_ft=1000.0,
    )
    assert probability < 1e-12


def test_the_sigma_cutoff_is_where_the_series_stops_mattering() -> None:
    """Documents the constant rather than leaving it a magic number.

    At the cutoff the normal tail is already below 1e-18, so returning exactly
    0 or 1 there is far more precise than the covariance feeding it.
    """
    tail = 0.5 * math.erfc(SIGMA_CUTOFF / math.sqrt(2.0))
    assert tail < 1e-18
