"""Pre-trade guardrails. Every order path must call an `assert_*can_trade` function and `assert_within_limits`."""

from .config import Settings, TradingMode

# IB paper accounts: DU... (individual), DF... (advisor master)
PAPER_ACCOUNT_PREFIXES = ("DU", "DF")


class OrderBlockedError(RuntimeError):
    """Raised when a safety rule blocks an order. Never catch this to 'retry anyway'."""


def is_paper_account(account_id: str) -> bool:
    return account_id.upper().startswith(PAPER_ACCOUNT_PREFIXES)


def assert_account_matches_mode(settings: Settings, account_ids: list[str], mode: TradingMode | None = None) -> None:
    """Refuse to work with a live account while in paper mode, and vice versa."""
    mode = mode or settings.mode
    if not account_ids:
        raise OrderBlockedError("Gateway reported no managed accounts.")
    for acct in account_ids:
        paper = is_paper_account(acct)
        if mode is TradingMode.PAPER and not paper:
            raise OrderBlockedError(f"Expected a PAPER session but account {acct} looks LIVE.")
        if mode is TradingMode.LIVE and paper:
            raise OrderBlockedError(f"Expected a LIVE session but account {acct} is a paper account.")
    if settings.account and mode is settings.mode and settings.account not in account_ids:
        raise OrderBlockedError(f"IB_ACCOUNT={settings.account} not among connected accounts {account_ids}.")


def assert_can_trade(settings: Settings, account_ids: list[str]) -> None:
    """CLI order path (uses IB_MODE)."""
    if settings.kill_switch_file.exists():
        raise OrderBlockedError(f"Kill switch active ({settings.kill_switch_file.name} exists).")
    if settings.readonly:
        raise OrderBlockedError("IB_READONLY=true: order sending is disabled.")
    if settings.mode is TradingMode.LIVE and not settings.allow_live_orders:
        raise OrderBlockedError("Live mode requires IB_ALLOW_LIVE_ORDERS=true.")
    assert_account_matches_mode(settings, account_ids)


def session_orders_enabled(settings: Settings, mode: TradingMode) -> tuple[bool, str | None]:
    """Whether the dashboard may send orders to this session, and if not, why."""
    if settings.kill_switch_file.exists():
        return False, f"Kill switch active ({settings.kill_switch_file.name} exists)."
    if mode is TradingMode.PAPER:
        return (True, None) if settings.paper_orders else (False, "IB_PAPER_ORDERS=false.")
    if settings.readonly or not settings.allow_live_orders:
        return False, "Live orders are disabled (need IB_READONLY=false and IB_ALLOW_LIVE_ORDERS=true in .env)."
    return True, None


def assert_session_can_trade(settings: Settings, mode: TradingMode, account_ids: list[str]) -> None:
    """Dashboard order path: per-session (paper/live) rules plus the account-type check."""
    ok, reason = session_orders_enabled(settings, mode)
    if not ok:
        raise OrderBlockedError(reason)
    assert_account_matches_mode(settings, account_ids, mode)


DERIVATIVE_TYPES = {"FUT", "OPT", "FOP", "BAG", "CONTFUT", "WAR", "CFD"}


def assert_within_limits(settings: Settings, quantity: float, max_cost: float, sec_type: str = "BAG") -> None:
    """Order value limit always applies; the contract-count limit applies to derivatives only."""
    if quantity <= 0:
        raise OrderBlockedError("Quantity must be positive.")
    if sec_type in DERIVATIVE_TYPES and quantity > settings.max_contracts_per_order:
        raise OrderBlockedError(
            f"Quantity {quantity:g} exceeds IB_MAX_CONTRACTS_PER_ORDER={settings.max_contracts_per_order}."
        )
    if max_cost > settings.max_debit_per_order:
        raise OrderBlockedError(
            f"Order value {max_cost:,.2f} exceeds IB_MAX_DEBIT_PER_ORDER={settings.max_debit_per_order:,.2f}."
        )
