from btc_risk_governor import BTCRiskGovernor, BTCRiskBlocked


def test_btc_governor_is_disabled_and_safe_by_default():
    g = BTCRiskGovernor()
    s = g.snapshot()
    assert s["enabled"] is False
    assert s["enabled"] is False
    assert s["stale"] is True
    assert g.scale_buy_amount(10) == 10
    assert g.scale_buy_quantity(3) == 3
    assert g.assert_entry_allowed("شراء") is None


def test_disabled_governor_never_blocks_or_reduces_buy():
    g = BTCRiskGovernor()
    assert g.scale_buy_amount(100) == 100
    assert g.scale_buy_quantity(2.5) == 2.5


def test_disabled_governor_exposes_compatibility_status():
    s = g = BTCRiskGovernor()
    assert g.portfolio_exposure_multiplier() == 1.0
    assert g.portfolio_risk_level(0, stale=True) == 4
