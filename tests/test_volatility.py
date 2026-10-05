import math
import random

import pytest

from oi_dashboard.analytics import vol_engine
from oi_dashboard.analytics.realized_vol import yang_zhang


def _random_walk(days=40, daily_vol=0.008, seed=7):
    rng = random.Random(seed)
    price, rows = 24000.0, []
    for _ in range(days):
        o = price * (1 + rng.gauss(0, daily_vol / 3))
        c = o * (1 + rng.gauss(0, daily_vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, daily_vol / 3)))
        low = min(o, c) * (1 - abs(rng.gauss(0, daily_vol / 3)))
        rows.append({"open": o, "high": h, "low": low, "close": c})
        price = c
    return rows


def test_yang_zhang_needs_enough_history():
    assert yang_zhang(_random_walk(days=10), window=20) is None


def test_yang_zhang_is_in_a_sane_annualised_range():
    vol = yang_zhang(_random_walk(), window=20)
    assert vol is not None
    assert 0.05 < vol < 0.40


def test_yang_zhang_scales_with_underlying_volatility():
    calm = yang_zhang(_random_walk(daily_vol=0.004), window=20)
    wild = yang_zhang(_random_walk(daily_vol=0.016), window=20)
    assert wild > calm


def test_standalone_and_server_estimators_agree():
    rows = _random_walk()
    assert vol_engine.yang_zhang(rows, window=20) == pytest.approx(yang_zhang(rows, window=20))


def test_annualisation_uses_252_trading_days():
    rows = _random_walk(daily_vol=0.01)
    closes = [r["close"] for r in rows]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])][-20:]
    mean = sum(rets) / len(rets)
    close_to_close = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) * 252)
    assert yang_zhang(rows, window=20) == pytest.approx(close_to_close, rel=0.6)
