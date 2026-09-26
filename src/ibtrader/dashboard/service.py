"""Owns the dashboard's single IB connection. READ-ONLY: this module never places, modifies or cancels orders."""

import asyncio
import datetime as dt
import logging
import math
import time
from collections import deque

from ib_async import IB, ContFuture, Contract, Future, Ticker

from ..analysis import costs, flex
from ..config import Settings
from ..safety import OrderBlockedError, assert_account_matches_mode
from .watchlist import Product, Watchlist

log = logging.getLogger(__name__)

INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 2100, 2150}  # "data farm OK" etc.
SUMMARY_TAGS = (
    "NetLiquidation", "TotalCashValue", "GrossPositionValue", "BuyingPower",
    "AvailableFunds", "MaintMarginReq", "UnrealizedPnL", "RealizedPnL",
)
RANGES = {  # key: (durationStr, barSize)
    "5D": ("5 D", "30 mins"), "1M": ("1 M", "4 hours"), "3M": ("3 M", "1 day"), "6M": ("6 M", "1 day"),
    "1Y": ("1 Y", "1 day"), "2Y": ("2 Y", "1 day"), "5Y": ("5 Y", "1 week"),
}
HIST_TTL = 15 * 60


def num(x) -> float | None:
    """JSON-safe number: IB uses nan / -1 / UNSET_DOUBLE for 'no value'."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) or abs(x) > 1e300 else x


def pos(x) -> float | None:
    x = num(x)
    return x if x is not None and x > 0 else None


class DashboardService:
    def __init__(self, settings: Settings):
        self.s = settings
        self.ib = IB()
        self.watchlist = Watchlist.load(settings.watchlist_file)
        self.products: dict[str, Product] = {p.key: p for p in self.watchlist.products}
        self.market_contracts: dict[str, Contract] = {}
        self.hist_contracts: dict[str, Contract] = {}
        self.tickers: dict[str, Ticker] = {}
        self.account: str | None = None
        self.pnl = None
        self.errors: deque[dict] = deque(maxlen=40)
        self.last_connect_error: str | None = None
        self._hist_cache: dict[tuple, tuple[float, list]] = {}
        self._hist_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.flex_data: dict | None = None
        self.fallback_close: dict[str, tuple[float, str]] = {}
        self.fx_ticker: Ticker | None = None
        self.fx_fallback: float | None = None
        self.ib.errorEvent += self._on_error

    # ---------- lifecycle ----------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.ib.disconnect()

    async def _run(self) -> None:
        while True:
            if not self.ib.isConnected():
                try:
                    await self.ib.connectAsync(
                        self.s.host, self.s.port, clientId=self.s.dashboard_client_id,
                        readonly=True, account=self.s.account or "", timeout=20,
                    )
                    assert_account_matches_mode(self.s, self.ib.managedAccounts())
                    await self._after_connect()
                    self.last_connect_error = None
                    log.info("Dashboard connected to %s:%s", self.s.host, self.s.port)
                except OrderBlockedError as e:
                    self.last_connect_error = f"Safety check failed: {e}"
                    self.ib.disconnect()
                except Exception as e:  # noqa: BLE001 - keep retrying, surface the error in the UI
                    self.last_connect_error = f"{type(e).__name__}: {e}"
                    self.ib.disconnect()
            await asyncio.sleep(10)

    async def _after_connect(self) -> None:
        self.account = self.s.account or self.ib.managedAccounts()[0]
        self.ib.reqMarketDataType(self.s.market_data_type)
        self.pnl = self.ib.reqPnL(self.account)
        await self.ib.accountSummaryAsync(self.account)
        self.tickers.clear()
        for p in self.products.values():
            try:
                await self._resolve(p)
                self.tickers[p.key] = self.ib.reqMktData(self.market_contracts[p.key])
            except Exception as e:  # noqa: BLE001
                self.errors.append({"time": _now(), "code": None, "msg": f"{p.key}: {e}", "symbol": p.key})
        if self.watchlist.fx:
            fx = Contract(**self.watchlist.fx["contract"])
            await self.ib.qualifyContractsAsync(fx)
            if fx.conId:
                self.fx_ticker = self.ib.reqMktData(fx)
        asyncio.create_task(self._load_fallback_closes())

    async def _load_fallback_closes(self) -> None:
        """Last daily close per product, for products without a (live or delayed) market data subscription."""
        try:
            fx = await self._fx_history("5D")
            if fx:
                self.fx_fallback = fx[max(fx)]
        except Exception:  # noqa: BLE001, S110
            pass
        for key in self.hist_contracts:
            try:
                bars = await self.history(key, "5D")
                if bars:
                    self.fallback_close[key] = (bars[-1]["c"], bars[-1]["t"][:10])
            except Exception:  # noqa: BLE001, S112
                continue

    async def _resolve(self, p: Product) -> None:
        spec = dict(p.contract)
        if spec.get("secType") == "CONTFUT":
            cont = ContFuture(spec["symbol"], exchange=spec.get("exchange", ""), currency=spec.get("currency", ""))
            await self.ib.qualifyContractsAsync(cont)
            if not cont.conId:
                raise ValueError("could not resolve continuous future")
            front = Future(conId=cont.conId)
            await self.ib.qualifyContractsAsync(front)
            self.hist_contracts[p.key], self.market_contracts[p.key] = cont, front
        else:
            c = Contract(**spec)
            await self.ib.qualifyContractsAsync(c)
            if not c.conId:
                raise ValueError("could not resolve contract")
            self.hist_contracts[p.key] = self.market_contracts[p.key] = c

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        if errorCode in INFO_CODES:
            return
        sym = getattr(contract, "symbol", None) if contract else None
        self.errors.append({"time": _now(), "code": errorCode, "msg": errorString, "symbol": sym})

    # ---------- helpers ----------
    def _account_values(self) -> list:
        return self.ib.accountValues(self.account) if self.account else []

    def base_currency(self) -> str | None:
        for v in self._account_values():
            if v.tag == "NetLiquidation" and v.currency not in ("", "BASE"):
                return v.currency
        return None

    def fx_to_base(self) -> dict[str, float]:
        """{currency: rate} so that amount_ccy * rate = amount_base."""
        rates = {}
        for v in self._account_values():
            if v.tag == "ExchangeRate" and v.currency and v.currency != "BASE":
                r = num(v.value)
                if r:
                    rates[v.currency] = r
        base = self.base_currency()
        if base:
            rates.setdefault(base, 1.0)
        # IB only reports rates for currencies held; fill the gap from the configured FX pair (e.g. EUR.USD).
        fx = self.watchlist.fx["contract"] if self.watchlist.fx else None
        if fx and base:
            t = self.fx_ticker
            bid, ask = (pos(t.bid), pos(t.ask)) if t else (None, None)
            px = ((bid + ask) / 2 if bid and ask else None) or (pos(t.close) if t else None) or self.fx_fallback
            sym, quote = fx["symbol"], fx["currency"]
            if px and base == sym:
                rates.setdefault(quote, 1 / px)
            elif px and base == quote:
                rates.setdefault(sym, px)
        return rates

    # ---------- status ----------
    def status(self) -> dict:
        acct = self.account or ""
        return {
            "connected": self.ib.isConnected(),
            "mode": self.s.mode.value,
            "readonly": True,
            "host": f"{self.s.host}:{self.s.port}",
            "account": f"{acct[:2]}•••{acct[-3:]}" if len(acct) > 5 else acct,
            "base_currency": self.base_currency(),
            "market_data_type": self.s.market_data_type,
            "connect_error": self.last_connect_error,
            "errors": list(self.errors)[-15:][::-1],
            "flex_configured": bool(self.s.flex_token and self.s.flex_query_id),
            "server_time": _now(),
        }

    # ---------- portfolio ----------
    def portfolio(self) -> dict:
        base = self.base_currency()
        fx = self.fx_to_base()
        vals = self._account_values()
        summary = {}
        for tag in SUMMARY_TAGS:
            match = [v for v in vals if v.tag == tag and v.currency in (base, "BASE")]
            if match:
                summary[tag] = num(match[0].value)
        cash = []
        for tag in ("TotalCashBalance", "$LEDGER-TotalCashBalance", "CashBalance", "$LEDGER-CashBalance"):
            cash = [
                {"currency": v.currency, "amount": num(v.value), "amount_base": num(v.value) * fx.get(v.currency, 0)}
                for v in vals
                if v.tag == tag and v.currency not in ("BASE", "") and num(v.value)
            ]
            if cash:
                break
        nlv = summary.get("NetLiquidation") or 0
        rows = []
        for it in self.ib.portfolio(self.account or ""):
            c = it.contract
            rate = fx.get(c.currency)
            mv_base = num(it.marketValue) * rate if rate and num(it.marketValue) is not None else None
            cost_basis = num(it.averageCost) * it.position if num(it.averageCost) is not None else None
            upnl = num(it.unrealizedPNL)
            rows.append({
                "symbol": c.symbol, "sec_type": c.secType,
                "local_symbol": (
                    f"{c.symbol} {c.lastTradeDateOrContractMonth}".strip() if c.secType == "BOND"
                    else c.localSymbol or c.symbol
                ),
                "currency": c.currency, "exchange": c.primaryExchange or c.exchange,
                "position": it.position, "avg_cost": num(it.averageCost), "market_price": num(it.marketPrice),
                "market_value": num(it.marketValue), "market_value_base": mv_base,
                "unrealized_pnl": upnl, "realized_pnl": num(it.realizedPNL),
                "unrealized_pct": (upnl / abs(cost_basis) * 100) if upnl is not None and cost_basis else None,
                "weight": (mv_base / nlv * 100) if mv_base is not None and nlv else None,
            })
        rows.sort(key=lambda r: -(abs(r["market_value_base"] or 0)))
        pnl = {
            "daily": num(self.pnl.dailyPnL) if self.pnl else None,
            "unrealized": num(self.pnl.unrealizedPnL) if self.pnl else None,
            "realized": num(self.pnl.realizedPnL) if self.pnl else None,
        }
        return {"base_currency": base, "summary": summary, "pnl": pnl, "cash": cash, "positions": rows}

    # ---------- gold quotes ----------
    def _quote(self, key: str) -> dict:
        p, t = self.products[key], self.tickers.get(key)
        c = self.market_contracts.get(key)
        bid, ask = (pos(t.bid), pos(t.ask)) if t else (None, None)
        last, close = (pos(t.last), pos(t.close)) if t else (None, None)
        mid = (bid + ask) / 2 if bid and ask else None
        hist, hist_date = self.fallback_close.get(key, (None, None))
        price = mid or last or close or hist
        source = "mid" if mid else "last" if last else "close" if close else "history" if hist else None
        close = close or hist
        live_spread = (ask - bid) if bid and ask else None
        return {
            "key": key, "name": p.name, "kind": p.kind, "currency": (c.currency if c else p.contract.get("currency")),
            "local_symbol": (c.localSymbol if c else None) or key,
            "expiry": (c.lastTradeDateOrContractMonth if c and p.kind == "future" else None),
            "bid": bid, "ask": ask, "last": last, "close": close, "price": price,
            "price_source": source, "history_date": hist_date if source == "history" else None,
            "change_pct": (price / close - 1) * 100 if price and close and source in ("mid", "last") else None,
            "spread": live_spread if live_spread is not None else p.typical_spread,
            "spread_source": "live" if live_spread is not None else "assumed",
            "market_data_type": t.marketDataType if t else None,
            "multiplier": p.multiplier,
        }

    def gold_quotes(self) -> dict:
        fx = self.fx_to_base()
        base = self.base_currency()
        quotes = [self._quote(k) for k in self.products]
        ref = next((q for q in quotes if q["key"] == self.watchlist.reference), None)
        ref_base = ref["price"] * fx[ref["currency"]] if ref and ref["price"] and ref["currency"] in fx else None
        for q in quotes:
            rate = fx.get(q["currency"])
            q["price_base"] = q["price"] * rate if q["price"] and rate else None
            q["notional_unit_base"] = q["price_base"] * q["multiplier"] if q["price_base"] else None
            q["oz_per_unit"] = q["notional_unit_base"] / ref_base if q["notional_unit_base"] and ref_base else None
            q["spread_bps"] = q["spread"] / q["price"] * 1e4 if q["price"] and q["spread"] is not None else None
            q["carry_annual_pct"] = None
            if q["kind"] == "future" and q["expiry"] and ref and ref["price"] and q["price"]:
                days = (dt.datetime.strptime(q["expiry"][:8], "%Y%m%d").date() - dt.date.today()).days
                fut_px, spot_px = q["price"], ref["price"]
                live = {"mid", "last"}
                if not (q["price_source"] in live and ref["price_source"] in live):
                    # Market closed / no live data: compare closes from the SAME day, never mixed snapshots.
                    fh, sh = self.fallback_close.get(q["key"]), self.fallback_close.get(ref["key"])
                    fut_px, spot_px = (fh[0], sh[0]) if fh and sh and fh[1] == sh[1] else (None, None)
                carry = costs.implied_annual_carry(fut_px, spot_px, days) if fut_px else None
                q["days_to_expiry"] = days
                q["carry_annual_pct"] = carry * 100 if carry is not None else None
        return {"base_currency": base, "reference": self.watchlist.reference, "quotes": quotes}

    # ---------- history ----------
    async def history(self, key: str, range_key: str) -> list[dict]:
        if key not in self.products or range_key not in RANGES:
            raise KeyError(key)
        cache_key = (key, range_key)
        hit = self._hist_cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < HIST_TTL:
            return hit[1]
        async with self._hist_lock:  # serialize requests: IB paces historical data
            contract = self.hist_contracts.get(key)
            if contract is None:
                raise ConnectionError("not connected / contract not resolved")
            duration, bar = RANGES[range_key]
            bars = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract, "", duration, bar, self.products[key].what_to_show, useRTH=True, formatDate=2
                ),
                timeout=40,
            )
        data = [{"t": b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date), "c": b.close} for b in bars]
        self._hist_cache[cache_key] = (time.monotonic(), data)
        return data

    async def tracking(self, range_key: str = "1Y") -> list[dict]:
        """ETF/ETC return vs spot gold in the same currency over the window."""
        ref_key = self.watchlist.reference
        ref = await self.history(ref_key, range_key)
        if not ref:
            return []
        eurusd = await self._fx_history(range_key)
        out = []
        for key, p in self.products.items():
            if p.kind != "etf":
                continue
            try:
                h = await self.history(key, range_key)
            except Exception as e:  # noqa: BLE001
                out.append({"key": key, "error": str(e)})
                continue
            if len(h) < 2:
                continue
            ref_map = {r["t"][:10]: r["c"] for r in ref}
            common = [r for r in h if r["t"][:10] in ref_map]
            if len(common) < 2:
                continue
            a, b = common[0], common[-1]
            rs, re_ = ref_map[a["t"][:10]], ref_map[b["t"][:10]]
            ccy = p.contract.get("currency", "USD")
            if ccy == "EUR" and eurusd:  # compare EUR product with gold priced in EUR
                fa, fb = eurusd.get(a["t"][:10]), eurusd.get(b["t"][:10])
                if not (fa and fb):
                    continue
                rs, re_ = rs / fa, re_ / fb
            td = costs.tracking_difference(a["c"], b["c"], rs, re_)
            years = max((dt.date.fromisoformat(b["t"][:10]) - dt.date.fromisoformat(a["t"][:10])).days / 365, 1e-9)
            out.append({
                "key": key, "name": p.name, "from": a["t"][:10], "to": b["t"][:10],
                "product_return_pct": (b["c"] / a["c"] - 1) * 100, "gold_return_pct": (re_ / rs - 1) * 100,
                "tracking_diff_pct": td * 100 if td is not None else None,
                "tracking_diff_annual_pct": ((1 + td) ** (1 / years) - 1) * 100 if td is not None else None,
                "expense_ratio_pct": p.expense_ratio * 100,
            })
        return out

    async def _fx_history(self, range_key: str) -> dict[str, float]:
        if not self.watchlist.fx:
            return {}
        key = ("__fx__", range_key)
        hit = self._hist_cache.get(key)
        if hit and time.monotonic() - hit[0] < HIST_TTL:
            return dict(hit[1])
        c = Contract(**self.watchlist.fx["contract"])
        async with self._hist_lock:
            await self.ib.qualifyContractsAsync(c)
            duration, bar = RANGES[range_key]
            bars = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(c, "", duration, bar, "MIDPOINT", useRTH=True, formatDate=2), 40
            )
        data = [(str(b.date)[:10], b.close) for b in bars]
        self._hist_cache[key] = (time.monotonic(), data)
        return dict(data)

    # ---------- costs ----------
    def cost_estimates(self, target_base: float) -> dict:
        fx = self.fx_to_base()
        quotes = {q["key"]: q for q in self.gold_quotes()["quotes"]}
        rows = []
        for key, p in self.products.items():
            q = quotes[key]
            if p.kind == "spot" or not q["price"] or q["currency"] not in fx:
                continue
            rate = fx[q["currency"]]
            qty = costs.units_for_exposure(target_base / rate, q["price"], p.multiplier, p.kind)
            rt = costs.round_trip(p.commission, qty, q["price"], p.multiplier, q["spread"] or 0)
            roll = costs.round_trip(p.commission, qty, q["price"], p.multiplier, p.typical_spread)
            hold = costs.annual_holding_cost(p.kind, rt.notional, p.expense_ratio, p.rolls_per_year, roll.total)
            rows.append({
                "key": key, "name": p.name, "kind": p.kind, "currency": q["currency"],
                "unit_notional_base": q["price"] * p.multiplier * rate,
                "qty": qty, "exposure_base": rt.notional * rate,
                "commission_rt_base": rt.commission * rate, "spread_rt_base": rt.spread * rate,
                "round_trip_base": rt.total * rate, "round_trip_bps": rt.bps,
                "annual_hold_base": hold * rate,
                "annual_hold_bps": hold / rt.notional * 1e4 if rt.notional else None,
                "first_year_base": (rt.total + hold) * rate,
                "spread_source": q["spread_source"],
                "carry_annual_pct": q.get("carry_annual_pct"),
                "note": None if qty else f"Minimum size is 1 unit ≈ {q['price'] * p.multiplier * rate:,.0f}",
            })
        return {"base_currency": self.base_currency(), "target": target_base, "rows": rows}

    def executions(self) -> dict:
        """Recent fills known to the Gateway (typically today / last days). Full history: Flex."""
        fx = self.fx_to_base()
        rows = []
        for f in self.ib.fills():
            c, e, cr = f.contract, f.execution, f.commissionReport
            rate = fx.get(c.currency, 1.0)
            mult = float(c.multiplier or 1)
            rows.append({
                "date": e.time.strftime("%Y-%m-%d") if e.time else "", "symbol": c.symbol, "asset": c.secType,
                "currency": c.currency, "side": e.side, "qty": e.shares, "price": e.price,
                "notional_base": abs(e.shares * e.price * mult) * rate,
                "commission_base": (num(cr.commission) or 0) * fx.get(cr.currency or c.currency, rate),
            })
        return {"rows": rows, "summary": costs.summarize_trades(rows)}

    async def refresh_flex(self) -> dict:
        if not (self.s.flex_token and self.s.flex_query_id):
            raise RuntimeError("Flex not configured: set IB_FLEX_TOKEN and IB_FLEX_QUERY_ID in .env")
        xml = await asyncio.to_thread(flex.fetch_statement, self.s.flex_token.get_secret_value(), self.s.flex_query_id)
        self.s.data_dir.mkdir(exist_ok=True)
        (self.s.data_dir / "flex_latest.xml").write_text(xml, encoding="utf-8")
        self.flex_data = None
        return self.flex_summary()

    def flex_summary(self) -> dict | None:
        if self.flex_data is None:
            path = self.s.data_dir / "flex_latest.xml"
            if not path.exists():
                return None
            xml = path.read_text(encoding="utf-8")
            trades, fees = flex.parse_trades(xml), flex.parse_fees(xml)
            fee_by_type: dict[str, float] = {}
            for r in fees:
                fee_by_type[r["type"]] = fee_by_type.get(r["type"], 0) + r["amount_base"]
            self.flex_data = {
                "as_of": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),
                "summary": costs.summarize_trades(trades),
                "fees": [{"type": k, "amount": v} for k, v in sorted(fee_by_type.items(), key=lambda x: -x[1])],
                "first": min((r["date"] for r in trades), default=None),
                "last": max((r["date"] for r in trades), default=None),
            }
        return self.flex_data


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
