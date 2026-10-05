from oi_dashboard.core.util import parse_date_safe, safe_sub
from oi_dashboard.trading.accounting import compute_trade_costs


def _trade(**kw):
    base = {
        "lots": 10, "lot_size_used": 65, "trade_type": "IC", "net_credit_entry": 30.0,
        "pnl_per_lot": 650.0, "pnl_total": 6500.0,
    }
    base.update(kw)
    return base


def test_costs_are_stamped_and_net_pnl_is_lower_than_gross():
    trade = _trade()
    compute_trade_costs(trade)
    assert trade["costs_total"] > 0
    assert trade["pnl_net"] == round(trade["pnl_total"] - trade["costs_total"], 2)


def test_costs_are_idempotent():
    trade = _trade()
    compute_trade_costs(trade)
    first = dict(trade)
    compute_trade_costs(trade)
    assert trade == first


def test_iron_condor_costs_more_than_a_two_leg_spread():
    ic, spread = _trade(trade_type="IC"), _trade(trade_type="GAP_SPREAD")
    compute_trade_costs(ic)
    compute_trade_costs(spread)
    assert ic["costs_total"] > spread["costs_total"]


def test_safe_sub():
    assert safe_sub(5, 3) == 2
    assert safe_sub(None, 3) == 0
    assert safe_sub(5, None, default=-1) == -1


def test_parse_date_safe_accepts_common_formats():
    assert str(parse_date_safe("2026-10-05")) == "2026-10-05"
    assert str(parse_date_safe("05-10-2026")) == "2026-10-05"
    assert str(parse_date_safe("05/10/2026")) == "2026-10-05"
    assert parse_date_safe("not a date") is None
