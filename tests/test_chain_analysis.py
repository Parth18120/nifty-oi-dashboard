from oi_dashboard.analytics import chain_analysis as ca


def _row(strike, ce_oi, pe_oi):
    return {"strike": strike, "ce_oi": ce_oi, "pe_oi": pe_oi}


def test_pcr_is_put_oi_over_call_oi():
    chain = [_row(24000, 100, 150), _row(24100, 100, 50)]
    assert ca.compute_pcr(chain) == 1.0


def test_pcr_handles_empty_call_side():
    assert ca.compute_pcr([_row(24000, 0, 500)]) == 0.0


def test_pcr_label_bands():
    assert ca.pcr_label(1.6)[1] == "green"
    assert ca.pcr_label(0.9)[0] == "NEUTRAL"
    assert ca.pcr_label(0.5)[1] == "red"


def test_max_pain_sits_at_the_centre_of_a_symmetric_chain():
    chain = [_row(s, 1000, 1000) for s in range(23800, 24201, 100)]
    assert ca.compute_max_pain(chain) == 24000


def test_max_pain_moves_toward_heavy_open_interest():
    chain = [_row(23900, 10, 5000), _row(24000, 10, 10), _row(24100, 5000, 10)]
    assert ca.compute_max_pain(chain) == 24000
