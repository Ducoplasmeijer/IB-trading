# IB-trading

Personal Interactive Brokers toolkit: portfolio dashboard, metals analysis, guarded order entry, option spreads.
See [PLAN.md](PLAN.md).

## Setup (Windows)
1. Install [IB Gateway (stable)](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php).
2. In Gateway, open Configure → Settings → API → Settings:
   - Enable ActiveX and Socket Clients
   - Socket port **4002** for a paper login, **4001** for a live login
   - Tick **"Allow connections from localhost only"**, with Trusted IPs `127.0.0.1` only
   - Live login: keep **Read-Only API** ticked until you deliberately want live orders. Paper login: untick it.
3. Set up the project:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev]"
   copy .env.example .env
   pre-commit install
   ```

## Dashboard
```powershell
ibt dashboard          # then open http://127.0.0.1:8050
```
It runs on your own PC, next to IB Gateway, and binds to 127.0.0.1 only. **Never port-forward it or expose it
to the internet.** For access from your phone or laptop, use a private VPN such as Tailscale, or an SSH tunnel.

### Paper vs live sessions
The dashboard connects to **both** Gateway sessions when they are running: paper on port 4002 and live on 4001.
The **Paper / Live** switch in the top bar picks which one every tab shows and trades. A green dot means that
session is connected. The colored line at the top of the page shows which session you're in: blue for paper,
red for live.

To have both at once, start two Gateway logins: your live username on 4001 and your paper username on 4002.
You can run TWS for one and IB Gateway for the other, or two Gateway instances. With only one login running,
just use that session; the other shows "offline".

### Tabs
- **Portfolio**: net liquidation, P&L (today / unrealized / realized), positions, allocation, and cash per currency.
- **Metals**: gold, silver, copper, aluminium and nickel groups. Each has quotes, spreads, metal per unit, futures
  carry vs spot, an indexed history chart, and ETF/ETC tracking vs the reference price (spot, or the front future
  where no spot exists).
- **Trade**: order ticket (limit orders), L1 quote + order-book depth, and open/recent orders with cancel.
- **Trading costs**: cost to hold a target exposure per product (commission + spread + fees/rolls), your recent
  commissions, and your full history via Flex.

Products and cost assumptions live in [config/watchlist.yaml](config/watchlist.yaml). Add or remove metals there.

### Sending orders from the dashboard
1. Pick the **Paper** session, then an instrument in the Trade tab: a position, a metal product, or another stock/ETF.
2. Enter side, quantity and limit price. You can fill the price from bid/mid/ask/last.
3. **Preview order**. The server checks the guardrails and asks IB for a what-if (commission and margin impact).
   The preview is valid for 90 seconds.
4. **Submit**. Orders are checked again on submit, and every preview, submit and cancel is logged to `logs/orders.jsonl`.

Live orders stay disabled until you set `IB_READONLY=false` **and** `IB_ALLOW_LIVE_ORDERS=true` in `.env`, and untick
Read-Only API in the live Gateway. Even then, every live order needs you to type `LIVE`.

## Market data you can get from IB
| Data | API | Needs |
|---|---|---|
| Top of book (bid/ask/last/size) | `reqMktData` | Market data subscription per exchange; otherwise 15-min delayed |
| Order book / depth (L2) | `reqMktDepth` | Separate **depth** subscription (e.g. NYSE ArcaBook, CME depth); max ~3 books at once |
| Tick-by-tick trades / quotes | `reqTickByTickData` | Live subscription; limited number of simultaneous streams |
| 5-second real-time bars | `reqRealTimeBars` | Live subscription |
| Historical bars (1 s to 1 month) | `reqHistoricalData` | Usually works without a subscription; pacing limits apply |
| Historical ticks | `reqHistoricalTicks` | Live subscription |

Subscriptions are managed in Client Portal → Settings → Market Data Subscriptions. Paper accounts can share the
live account's subscriptions (a setting in Client Portal).

## CLI
```powershell
ibt check | ibt portfolio
ibt chain --symbol GC --sec-type FOP --exchange COMEX --trading-class OG
ibt spread preview --spec config/strategies/gold_call_spread.yaml
```

## Safety
- Defaults: the live session is read-only; paper orders are allowed (the account must be a `DU…` paper account).
- Each session checks that the account type matches (a live account on the paper port is refused).
- Per-order limits: `IB_MAX_DEBIT_PER_ORDER` (order value in base currency) and `IB_MAX_CONTRACTS_PER_ORDER`
  (derivatives). Limit orders only.
- Emergency stop: create a file named `KILL_SWITCH` in the project root. That blocks every order, paper and live.
