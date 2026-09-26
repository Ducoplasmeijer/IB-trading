# Plan

## Which IBKR API
| Option | How it works | Verdict |
|---|---|---|
| **TWS API via IB Gateway** (chosen) | Local socket (127.0.0.1:4002 paper / 4001 live). You log in with 2FA in the Gateway app. | Full order types incl. combos, streaming data, no credentials in code. Best fit. |
| Client Portal Web API (REST) | Local Java gateway plus a browser login, session expires daily | Clunkier auth, weaker combo support |
| Web API with OAuth | Hosted REST | Mainly for institutions/third parties; not needed |

Library: [`ib_async`](https://github.com/ib-api-reloaded/ib_async), the maintained successor of `ib_insync`. It does not need IB's own `ibapi` package.

## Security model (defense in depth)
1. **No IB credentials in the repo.** Login and 2FA happen in IB Gateway. `.env` holds only host, port and flags, and it is gitignored.
2. **Paper by default.** `IB_MODE=paper` and `IB_READONLY=true`. Live orders need `IB_MODE=live`, a live port, and `IB_ALLOW_LIVE_ORDERS=true`.
3. **Mode checks.** Startup fails if the port doesn't match the mode. After connecting, the code refuses to continue if the account type (DU… = paper) doesn't match the mode.
4. **Hard block in Gateway.** Tick "Read-Only API" in Gateway while you only want the dashboard. Only allow 127.0.0.1 in Trusted IPs.
5. **Per-order limits.** Max contracts and max debit per order, limit orders only, IB what-if preview before placing, and a typed `PLACE` confirmation.
6. **Kill switch.** While a file named `KILL_SWITCH` exists in the project root, every order is blocked.
7. **GitHub (public repo).** Only code and config go in git; no account IDs, positions, balances or `data/`. Gitleaks runs in CI and pre-commit, and `.claude/settings.json` blocks Claude from reading `.env` and `data/`.

## Phases
- [x] **0. Scaffold:** config, safety, connection, portfolio snapshot, vertical-spread combo builder, CLI, tests, CI.
- [x] **1. Connect:** IB Gateway connected (read-only). `ibt check` works.
- [x] **1b. Read-only dashboard:** `ibt dashboard` shows the portfolio feed, gold prices and analysis, and trading-cost analysis (see README).
- [ ] **1c. Flex history:** create the Flex query and token, and put them in `.env`, to get the full commission and fee history.
- [ ] **2. Gold call spread (paper):** pick GC futures options (COMEX, class OG, needs COMEX data) or GLD options (needs OPRA data). Run `ibt chain`, fill in `config/strategies/gold_call_spread.yaml`, run `ibt spread preview`, then place it on paper.
- [ ] **3. Local API service:** a FastAPI app on 127.0.0.1 that owns the single IB connection and exposes `/portfolio`, `/positions`, `/orders/preview` and `/orders` (token-protected, same guardrails). Your existing dashboard calls this.
- [ ] **4. Portfolio dashboard:** positions, P&L, margin, option Greeks, spread P&L chart. Read-only client ID.
- [ ] **5. Ops:** order and fill audit log (local, gitignored), IBC for automatic Gateway restarts, alerts.
- [ ] **6. Live:** only after the paper runs are verified. Start with small `IB_MAX_*` limits.

## Things to have ready on the IB side
- A paper trading account. It is enabled in Client Portal under Settings → Paper Trading Account.
- Market data subscriptions: CME/COMEX (for GC options) or OPRA (for GLD options). Paper accounts can share live data subscriptions (a setting in Client Portal).
- Futures/options trading permissions on the account.
