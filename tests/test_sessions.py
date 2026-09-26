import pytest

from ibtrader.config import PROJECT_ROOT, Settings, TradingMode
from ibtrader.dashboard.service import InstrumentRef, OrderRequest
from ibtrader.dashboard.watchlist import Watchlist
from ibtrader.safety import OrderBlockedError, assert_session_can_trade, assert_within_limits, session_orders_enabled

PAPER, LIVE = TradingMode.PAPER, TradingMode.LIVE


def make(tmp_path, **kw) -> Settings:
    kw.setdefault("kill_switch_file", tmp_path / "KILL_SWITCH")
    return Settings(_env_file=None, **kw)


def test_paper_orders_on_by_default_live_off(tmp_path):
    s = make(tmp_path)
    assert session_orders_enabled(s, PAPER) == (True, None)
    ok, why = session_orders_enabled(s, LIVE)
    assert not ok and "IB_ALLOW_LIVE_ORDERS" in why


def test_live_needs_both_flags(tmp_path):
    assert not session_orders_enabled(make(tmp_path, allow_live_orders=True), LIVE)[0]  # readonly still true
    assert not session_orders_enabled(make(tmp_path, readonly=False), LIVE)[0]
    assert session_orders_enabled(make(tmp_path, readonly=False, allow_live_orders=True), LIVE)[0]


def test_paper_session_refuses_live_account(tmp_path):
    with pytest.raises(OrderBlockedError, match="LIVE"):
        assert_session_can_trade(make(tmp_path), PAPER, ["U123456"])
    assert_session_can_trade(make(tmp_path), PAPER, ["DU123456"])


def test_live_session_refuses_paper_account(tmp_path):
    s = make(tmp_path, readonly=False, allow_live_orders=True)
    with pytest.raises(OrderBlockedError, match="paper"):
        assert_session_can_trade(s, LIVE, ["DU123456"])


def test_kill_switch_blocks_paper_too(tmp_path):
    s = make(tmp_path)
    s.kill_switch_file.touch()
    with pytest.raises(OrderBlockedError, match="Kill switch"):
        assert_session_can_trade(s, PAPER, ["DU123456"])


def test_contract_limit_only_for_derivatives(tmp_path):
    s = make(tmp_path, max_contracts_per_order=2, max_debit_per_order=5000)
    assert_within_limits(s, 100, 4000, "STK")  # 100 shares is fine
    with pytest.raises(OrderBlockedError):
        assert_within_limits(s, 3, 100, "FUT")
    with pytest.raises(OrderBlockedError, match="value"):
        assert_within_limits(s, 10, 6000, "STK")


def test_swapped_ports_rejected(tmp_path):
    with pytest.raises(ValueError):
        make(tmp_path, paper_port=4001)


def test_watchlist_config_is_valid():
    wl = Watchlist.load(PROJECT_ROOT / "config" / "watchlist.yaml")
    names = [g.name for g in wl.groups]
    assert names[:5] == ["Gold", "Silver", "Copper", "Aluminium", "Nickel"]
    assert all(p.group for p in wl.products.values())


def test_order_request_validation():
    with pytest.raises(ValueError):
        InstrumentRef()
    with pytest.raises(ValueError):
        InstrumentRef(symbol="GLD; DROP")
    with pytest.raises(ValueError):
        OrderRequest(instrument={"key": "GLD"}, action="BUY", quantity=0, limit_price=10)
    OrderRequest(instrument={"key": "GLD"}, action="SELL", quantity=1, limit_price=10)
