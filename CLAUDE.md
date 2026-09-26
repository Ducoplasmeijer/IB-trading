# IB-trading — project guide for Claude

Personal Interactive Brokers toolkit (Python, `ib_async`). Repo: https://github.com/Ducoplasmeijer/IB-trading

## Architecture
- IB Gateway/TWS runs locally; the user logs in there (2FA). Code only connects to `127.0.0.1` via the TWS API socket.
- `src/ibtrader/config.py` — settings from `.env` (`IB_*`), validated (mode vs port, localhost only).
- `src/ibtrader/safety.py` — guardrails. **Every order path must call `assert_can_trade` + `assert_within_limits`.**
- `src/ibtrader/connection.py` — `connect()` context manager; verifies paper/live account matches mode.
- `src/ibtrader/portfolio.py` — read-only snapshot. `src/ibtrader/strategies/` — order builders (vertical spread = BAG combo).
- `src/ibtrader/cli.py` — `ibt check | portfolio | chain | dashboard | spread preview|place`.
- `src/ibtrader/dashboard/` — FastAPI app (`app.py`) + `service.py` (`SessionManager` holding a paper and a live
  `Session`, each its own IB connection with auto-reconnect; every endpoint takes `?session=paper|live`)
  + vanilla JS/Chart.js frontend in `static/`. Order endpoints exist ONLY via `Session.preview_order` →
  `submit_order` (two steps, 90 s preview TTL, guardrails re-checked on submit, typed "LIVE" for live, audit log
  in `logs/orders.jsonl`). Don't add other order paths or weaken these checks.
- `src/ibtrader/analysis/` — `costs.py` (pure cost math, unit-tested) and `flex.py` (Flex Web Service history).
- `config/watchlist.yaml` — metals grouped per metal (reference product, unit) + cost assumptions (committed).
- IB quirks: account values arrive as `$LEDGER-*` tags; no `ExchangeRate` tag for currencies you don't hold (EUR.USD
  ticker fills the gap); products without a data subscription fall back to the last historical close.

## Rules
- The GitHub repo is PUBLIC. Never commit account IDs, positions, balances, `data/`, or screenshots of the dashboard.
- Never read, print, or commit `.env`, account numbers, or real strategy YAMLs (`config/strategies/*.yaml` except `*.example.yaml`).
- Never place orders on the user's behalf (not via CLI, not via the dashboard API). Claude may run read-only commands
  and may preview a live order only while live orders are disabled (then no IB call is made); `place`/submit is for the user.
- Do not weaken safety defaults (paper, readonly, no live orders) or bypass `OrderBlockedError`.
- Always use limit orders; no market orders on options/combos.
- Add a test in `tests/` for any new guardrail.

## Commands
```
.venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
ruff check .
```
