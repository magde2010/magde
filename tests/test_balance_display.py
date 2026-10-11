from mexc_client import MexcClient


def test_portfolio_value_includes_locked_balances(monkeypatch):
    client = MexcClient.__new__(MexcClient)
    client.quote = "USDT"

    monkeypatch.setattr(
        client,
        "get_total_balance",
        lambda: {"USDT": 10.0, "BTC": 0.001},
    )
    monkeypatch.setattr(
        client,
        "get_all_prices",
        lambda assets: {"BTC": 50000.0},
    )

    result = client.get_portfolio_value()

    assert result["total_usdt"] == 60.0
    assert result["assets"]["USDT"]["usdt_value"] == 10.0
    assert result["assets"]["BTC"]["usdt_value"] == 50.0


def test_total_balance_prefers_total_over_free(monkeypatch):
    client = MexcClient.__new__(MexcClient)
    client.exchange = type(
        "Exchange",
        (),
        {
            "fetch_balance": lambda self: {
                "total": {"BTC": 0.002, "USDT": 10},
                "free": {"BTC": 0.001, "USDT": 10},
            }
        },
    )()
    result = client.get_total_balance()
    assert result["BTC"] == 0.002
    assert result["USDT"] == 10
