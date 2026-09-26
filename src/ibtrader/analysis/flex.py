"""IBKR Flex Web Service: full trade and fee history (the TWS API only returns recent executions).

Setup (one time, in Client Portal): Performance & Reports > Flex Queries > Activity Flex Query with
sections "Trades" (incl. IB Commission, FX Rate To Base) and "Cash Transactions". Then enable
Flex Web Service to get a token. Put IB_FLEX_TOKEN and IB_FLEX_QUERY_ID in .env (never commit them).
"""

import time
import urllib.parse
import urllib.request

from defusedxml import ElementTree as ET

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
IN_PROGRESS_CODES = {"1019"}  # statement generation in progress
FEE_TYPES = {"Other Fees", "Commission Adjustments", "Broker Interest Paid", "Withholding Tax"}


class FlexError(RuntimeError):
    pass


def _get(url: str, params: dict, timeout: float) -> str:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "ibtrader/0.1"})  # noqa: S310 (fixed https host)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def fetch_statement(token: str, query_id: str, timeout: float = 30, max_wait: float = 120) -> str:
    """Blocking. Run it in a thread from async code."""
    root = ET.fromstring(_get(f"{BASE_URL}/SendRequest", {"t": token, "q": query_id, "v": "3"}, timeout))
    if root.findtext("Status") != "Success":
        raise FlexError(f"Flex SendRequest failed: {root.findtext('ErrorCode')} {root.findtext('ErrorMessage')}")
    ref, url = root.findtext("ReferenceCode"), root.findtext("Url") or f"{BASE_URL}/GetStatement"
    deadline = time.monotonic() + max_wait
    while True:
        text = _get(url, {"t": token, "q": ref, "v": "3"}, timeout)
        if not text.lstrip().startswith("<FlexStatementResponse"):
            return text  # the statement itself
        resp = ET.fromstring(text)
        if resp.findtext("ErrorCode") not in IN_PROGRESS_CODES or time.monotonic() > deadline:
            raise FlexError(f"Flex GetStatement failed: {resp.findtext('ErrorCode')} {resp.findtext('ErrorMessage')}")
        time.sleep(3)


def _f(el, name: str, default: float = 0.0) -> float:
    try:
        return float(el.get(name) or default)
    except ValueError:
        return default


def parse_trades(xml_text: str) -> list[dict]:
    """Normalized rows: commission_base is a positive cost in base currency."""
    rows = []
    for t in ET.fromstring(xml_text).iter("Trade"):
        if t.get("levelOfDetail", "EXECUTION") not in ("EXECUTION", ""):
            continue
        fx = _f(t, "fxRateToBase", 1.0) or 1.0
        mult = _f(t, "multiplier", 1.0) or 1.0
        rows.append(
            {
                "date": (t.get("tradeDate") or t.get("dateTime") or "")[:10],
                "symbol": t.get("symbol", ""),
                "asset": t.get("assetCategory", ""),
                "currency": t.get("currency", ""),
                "side": t.get("buySell", ""),
                "qty": _f(t, "quantity"),
                "price": _f(t, "tradePrice"),
                "notional_base": abs(_f(t, "quantity") * _f(t, "tradePrice") * mult) * fx,
                "commission_base": -_f(t, "ibCommission") * fx,
            }
        )
    return rows


def parse_fees(xml_text: str) -> list[dict]:
    rows = []
    for c in ET.fromstring(xml_text).iter("CashTransaction"):
        if c.get("type") in FEE_TYPES and _f(c, "amount") < 0:
            rows.append(
                {
                    "date": (c.get("dateTime") or c.get("reportDate") or "")[:10],
                    "type": c.get("type"),
                    "description": c.get("description", ""),
                    "amount_base": -_f(c, "amount") * (_f(c, "fxRateToBase", 1.0) or 1.0),
                }
            )
    return rows
