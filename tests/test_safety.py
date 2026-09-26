import pytest

from ibtrader.config import Settings, TradingMode
from ibtrader.safety import OrderBlockedError, assert_can_trade, assert_within_limits
from ibtrader.strategies.vertical_spread import SpreadSpec, max_cost, max_profit


def make(tmp_path, **kw) -> Settings:
    kw.setdefault("kill_switch_file", tmp_path / "KILL_SWITCH")
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe(tmp_path):
    s = make(tmp_path)
    assert s.mode is TradingMode.PAPER and s.readonly and not s.allow_live_orders


@pytest.mark.parametrize(("mode", "port"), [("paper", 4001), ("paper", 7496), ("live", 4002), ("live", 7497)])
def test_mode_port_mismatch_rejected(tmp_path, mode, port):
    with pytest.raises(ValueError):
        make(tmp_path, mode=mode, port=port)


def test_remote_host_rejected(tmp_path):
    with pytest.raises(ValueError):
        make(tmp_path, host="10.0.0.5")


def test_readonly_blocks(tmp_path):
    with pytest.raises(OrderBlockedError, match="READONLY"):
        assert_can_trade(make(tmp_path), ["DU123"])


def test_paper_mode_refuses_live_account(tmp_path):
    with pytest.raises(OrderBlockedError, match="LIVE"):
        assert_can_trade(make(tmp_path, readonly=False), ["U123"])


def test_live_needs_explicit_flag(tmp_path):
    s = make(tmp_path, mode="live", port=4001, readonly=False)
    with pytest.raises(OrderBlockedError, match="ALLOW_LIVE"):
        assert_can_trade(s, ["U123"])


def test_kill_switch(tmp_path):
    s = make(tmp_path, readonly=False)
    s.kill_switch_file.touch()
    with pytest.raises(OrderBlockedError, match="Kill switch"):
        assert_can_trade(s, ["DU123"])


def test_paper_ok(tmp_path):
    assert_can_trade(make(tmp_path, readonly=False), ["DU123"])


def test_limits(tmp_path):
    s = make(tmp_path, max_contracts_per_order=2, max_debit_per_order=1000)
    with pytest.raises(OrderBlockedError):
        assert_within_limits(s, 3, 100)
    with pytest.raises(OrderBlockedError):
        assert_within_limits(s, 1, 1500)
    assert_within_limits(s, 2, 999)


SPEC = dict(symbol="GC", sec_type="FOP", exchange="COMEX", expiry="20261124",
            long_strike=3000, short_strike=3100, limit_price=25, quantity=2)


def test_spread_math():
    spec = SpreadSpec(**SPEC)
    assert max_cost(spec, 100) == 5000
    assert max_profit(spec, 100) == 15000


@pytest.mark.parametrize("bad", [dict(long_strike=3100, short_strike=3000), dict(limit_price=0),
                                 dict(limit_price=150)])
def test_spread_validation(bad):
    with pytest.raises(ValueError):
        SpreadSpec(**{**SPEC, **bad})
