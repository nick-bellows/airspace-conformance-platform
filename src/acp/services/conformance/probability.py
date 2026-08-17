"""How likely is it that these two aircraft actually breach separation?

## Why this exists

The deterministic detector thresholds a point estimate: it projects both tracks
to the closest point of approach, and declares a conflict if the predicted miss
is under 5 NM laterally *and* 1000 ft vertically. A predicted 4.9 NM miss
alerts; 5.1 NM does not. The velocity estimate carries enough noise to move the
answer across that line, and the published precision of 0.57 is the bill for it.

The filter already knows how uncertain it is. It maintains a covariance, that
covariance grows during dropouts and manoeuvres, and the error in a *predicted*
position grows with the square of the lookahead because velocity error
integrates. None of that reached the decision. Here it does: the miss distance
becomes a distribution rather than a number, and the alert fires on the
probability of breach rather than on which side of a line the mean landed.

## The model

Both aircraft are propagated to the closest point of approach. The relative
position there is Gaussian, centred on the predicted relative position, with a
variance that is the sum of both tracks' position variance plus their velocity
variance scaled by the time to get there:

    sigma_h(t)^2 = sp1^2 + sp2^2 + t^2 * (sv1^2 + sv2^2)

Horizontal and vertical are treated as independent, which is what this filter
actually assumes -- its altitude channel is a separate block with no
cross-terms, so there is no correlation to throw away.

**Horizontal.** The relative position is a two-dimensional isotropic Gaussian
and the question is whether it lands inside a disc of radius 5 NM. That is the
non-central chi-squared distribution with two degrees of freedom, equivalently
the Rice distribution, equivalently a Marcum Q function.

**Vertical.** One dimension, so an ordinary normal integrated between -1000 and
+1000 ft.

The two multiply, because a conflict needs both.

## Computing it without scipy

The conformance service must run with only numpy -- `scipy` arrives with
scikit-learn, and the `degradation` CI job proves the service works with the ml
extra genuinely uninstalled. So the non-central chi-squared CDF is computed
here rather than imported.

It is not the naive series. Writing the non-central chi-squared with two
degrees of freedom as a Poisson mixture of central chi-squareds gives

    P = sum_j Pois(j; a^2/2) * [1 - PoisCDF(j; b^2/2)]

where `a` is the predicted miss in units of sigma and `b` is the standard in
units of sigma -- because a central chi-squared CDF with even degrees of
freedom *is* a Poisson survival function. The bracket is therefore
`P(N_b > j)`, and the whole sum collapses to a statement about two independent
Poisson variables:

    P(breach) = P(N_b > N_a),  N_a ~ Pois(a^2/2),  N_b ~ Pois(b^2/2)

That form has no factorials to overflow and no `exp(-a^2/2)` to underflow: both
Poisson laws are evaluated in log space. `tests/unit/test_probability.py`
checks it against `scipy.stats.ncx2` across four orders of magnitude, which is
the point of deriving it this way rather than approximating.

## What this does not model

Neither track's covariance is rotated into the along-track/cross-track frame,
so the position uncertainty is treated as isotropic when the filter's is
mildly elliptical. The error is small at this filter's tuning and is measured
in `docs/limitations.md` rather than assumed away. Turn rate is not propagated
either: a turning aircraft's future position is more uncertain than this says,
and the innovation signal is the system's separate answer to that.
"""

from __future__ import annotations

import math

import numpy as np

#: Beyond this many standard deviations the answer is 0 or 1 to far more
#: precision than any of the inputs deserve, and the series is skipped.
SIGMA_CUTOFF = 9.0

#: Poisson mass outside mean +/- this many standard deviations is dropped. At
#: 12 the truncation error is below 1e-15, well under the error in the
#: covariance that feeds this.
_POISSON_SPAN = 12.0


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _log_factorial(n: int) -> np.ndarray:
    """`[log 0!, log 1!, ... log n!]`.

    A cumulative sum of logs rather than `lgamma`, which numpy does not expose
    and which would mean depending on scipy for one function.
    """
    counts = np.arange(1, n + 1, dtype=np.float64)
    return np.concatenate([[0.0], np.cumsum(np.log(counts))])


def _poisson_log_pmf(k: np.ndarray, log_fact: np.ndarray, mean: float) -> np.ndarray:
    """log P(N = k) for N ~ Poisson(mean), stable for large means.

    Computed in logs throughout: at the means this reaches, `mean**k` overflows
    and `exp(-mean)` underflows long before their product does anything wrong.
    """
    if mean <= 0.0:
        return np.where(k == 0.0, 0.0, -np.inf)
    result: np.ndarray = k * math.log(mean) - mean - log_fact
    return result


def disc_probability(miss_nm: float, radius_nm: float, sigma_nm: float) -> float:
    """P(a 2-D isotropic Gaussian lands within `radius_nm` of the origin).

    The Gaussian is centred `miss_nm` from the origin with standard deviation
    `sigma_nm` in each axis. This is the non-central chi-squared CDF with two
    degrees of freedom; see the module docstring for why it is written as a
    race between two Poisson variables.
    """
    if radius_nm <= 0.0:
        return 0.0
    if sigma_nm <= 0.0:
        # A filter claiming zero uncertainty degrades to the deterministic test
        # rather than dividing by zero.
        return 1.0 if miss_nm < radius_nm else 0.0

    a = miss_nm / sigma_nm
    b = radius_nm / sigma_nm

    # Far outside the disc, or far inside it, to more precision than the
    # covariance justifies.
    if a - b > SIGMA_CUTOFF:
        return 0.0
    if b - a > SIGMA_CUTOFF:
        return 1.0

    mean_a = 0.5 * a * a
    mean_b = 0.5 * b * b

    # One index range wide enough for both Poisson laws.
    top = max(mean_a, mean_b)
    j_max = math.ceil(top + _POISSON_SPAN * math.sqrt(top + 1.0)) + 10
    j = np.arange(j_max + 1, dtype=np.float64)
    log_fact = _log_factorial(j_max)

    pmf_b = np.exp(_poisson_log_pmf(j, log_fact, mean_b))
    # P(N_b > j), accumulated from the top so the far tail is not lost to
    # cancellation against a cumulative sum that starts near 1.
    survival_b = np.concatenate([np.cumsum(pmf_b[::-1])[::-1][1:], [0.0]])

    pmf_a = np.exp(_poisson_log_pmf(j, log_fact, mean_a))
    return float(np.clip(np.dot(pmf_a, survival_b), 0.0, 1.0))


def band_probability(offset_ft: float, half_width_ft: float, sigma_ft: float) -> float:
    """P(a 1-D Gaussian centred at `offset_ft` lands within +/- `half_width_ft`)."""
    if half_width_ft <= 0.0:
        return 0.0
    if sigma_ft <= 0.0:
        return 1.0 if abs(offset_ft) < half_width_ft else 0.0
    upper = (half_width_ft - offset_ft) / sigma_ft
    lower = (-half_width_ft - offset_ft) / sigma_ft
    return max(0.0, _normal_cdf(upper) - _normal_cdf(lower))


def projected_sigma(
    sigma_now_a: float, sigma_now_b: float, rate_sigma_a: float, rate_sigma_b: float, dt_s: float
) -> float:
    """Uncertainty in a *relative* quantity after propagating `dt_s` seconds.

    Two independent tracks, so variances add; and the rate uncertainty
    integrates over the lookahead, which is why a conflict predicted five
    minutes out is far less certain than one predicted thirty seconds out even
    when both filters are equally confident right now.
    """
    positional = sigma_now_a**2 + sigma_now_b**2
    rate = (rate_sigma_a**2 + rate_sigma_b**2) * dt_s * dt_s
    return math.sqrt(max(0.0, positional + rate))


def conflict_probability(
    *,
    min_horizontal_nm: float,
    min_vertical_ft: float,
    horizontal_sigma_nm: float,
    vertical_sigma_ft: float,
    horizontal_standard_nm: float,
    vertical_standard_ft: float,
) -> float:
    """P(both standards are breached at closest approach).

    `min_vertical_ft` is the *signed* predicted altitude difference; its sign
    does not matter to the answer but passing the absolute value would be
    equally correct.
    """
    horizontal = disc_probability(min_horizontal_nm, horizontal_standard_nm, horizontal_sigma_nm)
    if horizontal <= 0.0:
        return 0.0
    vertical = band_probability(min_vertical_ft, vertical_standard_ft, vertical_sigma_ft)
    return horizontal * vertical
