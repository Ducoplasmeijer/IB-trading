import pytest

from ibtrader.analysis import costs, flex
from ibtrader.analysis.costs import CommissionModel


def test_commission_min_and_cap():
    m = CommissionModel(per_unit=0.005, min=1.0, max_pct=0.01)
    assert m.per_side(10, 400, 1) == 1.0  # 0.05 -> min 1.0
    assert m.per_side(1000, 400, 1) == pytest.approx(5.0)
    assert m.per_side(1, 50, 1) == pytest.approx(0.5)  # min capped at 1% of 50
    assert m.per_side(0, 400, 1) == 0


def test_round_trip_futures():
    rt = costs.round_trip(CommissionModel(per_unit=2.4), qty=1, price=4300, multiplier=100, spread=0.1)
    assert rt.notional == 430_000
    assert rt.commission == pytest.approx(4.8)
    assert rt.spread == pytest.approx(10)
    assert rt.bps == pytest.approx(14.8 / 430_000 * 1e4)


def test_units_for_exposure():
    assert costs.units_for_exposure(10_000, 390, 1, "etf") == 25
    assert costs.units_for_exposure(10_000, 4300, 100, "future") == 0
    assert costs.units_for_exposure(60_000, 4300, 10, "future") == 1


def test_holding_and_carry():
    assert costs.annual_holding_cost("etf", 10_000, expense_ratio=0.004) == pytest.approx(40)
    assert costs.annual_holding_cost("future", 43_000, rolls_per_year=6, cost_per_roll=3) == 18
    assert costs.implied_annual_carry(4336.5, 4300, 73) == pytest.approx(0.0424, abs=1e-3)
    assert costs.implied_annual_carry(4300, 4300, 0) is None


def test_tracking_difference():
    assert costs.tracking_difference(100, 109.6, 100, 110) == pytest.approx(-0.003636, abs=1e-5)


FLEX_XML = """<FlexQueryResponse><FlexStatements><FlexStatement>
<Trades>
 <Trade symbol="GLD" assetCategory="STK" currency="USD" fxRateToBase="0.9" tradeDate="20260910"
        quantity="10" tradePrice="390" multiplier="1" ibCommission="-1" buySell="BUY" levelOfDetail="EXECUTION"/>
 <Trade symbol="GLD" assetCategory="STK" currency="USD" fxRateToBase="0.9" tradeDate="20260915"
        quantity="-10" tradePrice="400" multiplier="1" ibCommission="-1" buySell="SELL" levelOfDetail="EXECUTION"/>
 <Trade symbol="MEUD" assetCategory="STK" currency="EUR" fxRateToBase="1" tradeDate="20260801"
        quantity="5" tradePrice="200" multiplier="1" ibCommission="-3" buySell="BUY" levelOfDetail="EXECUTION"/>
</Trades>
<CashTransactions>
 <CashTransaction type="Other Fees" amount="-2.5" fxRateToBase="1" dateTime="20260901" description="data"/>
 <CashTransaction type="Deposits/Withdrawals" amount="1000" fxRateToBase="1" dateTime="20260901"/>
</CashTransactions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_flex_parse_and_summarize():
    trades = flex.parse_trades(FLEX_XML)
    assert len(trades) == 3
    s = costs.summarize_trades(trades)
    assert s["total"]["commission"] == pytest.approx(0.9 + 0.9 + 3)
    assert s["total"]["notional"] == pytest.approx(3510 + 3600 + 1000)
    assert [m["key"] for m in s["by_month"]] == ["202608", "202609"]
    assert s["by_symbol"][0]["key"] == "MEUD"
    fees = flex.parse_fees(FLEX_XML)
    assert fees == [{"date": "20260901", "type": "Other Fees", "description": "data", "amount_base": 2.5}]
