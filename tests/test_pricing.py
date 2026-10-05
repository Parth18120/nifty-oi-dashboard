import math

import pytest

from oi_dashboard.analytics.pricing import bs_fair_price, overpricing_pct

SPOT, RV, DTE = 24000, 0.12, 5


def test_put_call_parity():
    strike = 24000
    call, _ = bs_fair_price(SPOT, strike, RV, DTE, "CE")
    put, _ = bs_fair_price(SPOT, strike, RV, DTE, "PE")
    from oi_dashboard.analytics.pricing import _fractional_dte

    t = _fractional_dte(DTE) / 365.0
    forward_gap = SPOT * math.exp(-0.012 * t) - strike * math.exp(-0.065 * t)
    assert call - put == pytest.approx(forward_gap, abs=0.5)


def test_delta_signs():
    _, call_delta = bs_fair_price(SPOT, 24000, RV, DTE, "CE")
    _, put_delta = bs_fair_price(SPOT, 24000, RV, DTE, "PE")
    assert 0 < call_delta < 1
    assert -1 < put_delta < 0


def test_far_otm_options_are_cheap_and_ordered():
    near, _ = bs_fair_price(SPOT, 24300, RV, DTE, "CE")
    far, _ = bs_fair_price(SPOT, 24800, RV, DTE, "CE")
    assert near > far >= 0.01


def test_higher_volatility_raises_price():
    low, _ = bs_fair_price(SPOT, 24300, 0.08, DTE, "CE")
    high, _ = bs_fair_price(SPOT, 24300, 0.20, DTE, "CE")
    assert high > low


def test_overpricing_pct_is_suppressed_for_untradeable_quotes():
    assert overpricing_pct(0.4, 5.0) is None
    assert overpricing_pct(10.0, 0.5) is None
    assert overpricing_pct(None, 5.0) is None


def test_overpricing_pct_value():
    assert overpricing_pct(12.0, 10.0) == 20.0
    assert overpricing_pct(8.0, 10.0) == -20.0
