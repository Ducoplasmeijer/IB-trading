# IB-trading

Personal Interactive Brokers toolkit: portfolio overview, guarded order entry, option spreads. See [PLAN.md](PLAN.md).

## Setup (Windows)
1. Install [IB Gateway (stable)](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php) and log in (paper or live).
2. In Gateway, open Configure → Settings → API → Settings:
   - Enable ActiveX and Socket Clients
   - Socket port **4002** (paper) or **4001** (live). It must match `IB_MODE`/`IB_PORT` in `.env`.
   - Tick **"Allow connections from localhost only"**, with Trusted IPs `127.0.0.1` only
   - Tick **Read-Only API** until you want to send orders
   - Tick "Download open orders on connection"
3. Set up the project:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev]"
   copy .env.example .env
   pre-commit install
   ```
4. Try it out:
   ```powershell
   ibt check
   ibt portfolio
   ibt chain --symbol GC --sec-type FOP --exchange COMEX --trading-class OG
   ibt spread preview --spec config/strategies/gold_call_spread.yaml
   ```

## Dashboard (read-only)
```powershell
ibt dashboard          # then open http://127.0.0.1:8050
```
- **Portfolio**: net liquidation, today's / unrealized / realized P&L, positions, allocation, cash per currency (live-updating).
- **Gold markets**: spot XAUUSD, COMEX GC/MGC (front month), GLD/IAU/GLDM, Xetra-Gold. Includes quotes, spreads, gold-per-unit, futures carry vs spot, an indexed history chart, and ETF tracking vs spot.
- **Trading costs**: for a target exposure, the round-trip commission + spread, annual holding cost (ETF fee / futures rolls) and year-1 total per product. Also your own commissions from recent executions, and your full history via Flex.
- Products and cost assumptions live in [config/watchlist.yaml](config/watchlist.yaml). Edit the commission numbers to match your IBKR tier.
- It always connects read-only (client ID 11, separate from order scripts), binds to 127.0.0.1 only, sends strict security headers, and has **no order endpoints**.
- Products without a market-data subscription fall back to the last historical close (marked in orange). Xetra (4GLD) needs a German/Xetra data subscription.

## Safety
- Defaults: paper mode, read-only, no live orders.
- To send paper orders: untick Read-Only API in Gateway **and** set `IB_READONLY=false`.
- To trade live: `IB_MODE=live`, `IB_PORT=4001`, and `IB_ALLOW_LIVE_ORDERS=true`.
- Emergency stop: create a file named `KILL_SWITCH` in the project root.
