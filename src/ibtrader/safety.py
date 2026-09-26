"""Pre-trade guardrails. Every order path must call `assert_can_trade` and `assert_within_limits`."""

from .config import Settings, TradingMode

# IB paper accounts: DU... (individual), DF... (advisor master)
PAPER_ACCOUNT_PREFIXES = ("DU", "DF")


class OrderBlockedError(RuntimeError):
    """Raised when a safety rule blocks an order. Never catch this to 'retry anyway'."""


def is_paper_account(account_id: str) -> bool:
    return account_id.upper().startswith(PAPER_ACCOUNT_PREFIXES)


def assert_account_matches_mode(settings: Settings, account_ids: list[str]) -> None:
    """Refuse to work with a live account while in paper mode, and vice versa."""
    if not account_ids:
        raise OrderBlockedError("Gateway reported no managed accounts.")
    for acct in account_ids:
        paper = is_paper_account(acct)
        if settings.mode is TradingMode.PAPER and not paper:
            raise OrderBlockedError(f"IB_MODE=paper but connected account {acct} looks LIVE.")
        if settings.mode is TradingMode.LIVE and paper:
            raise OrderBlockedError(f"IB_MODE=live but connected account {acct} is a paper account.")
    if settings.account and settings.account not in account_ids:
        raise OrderBlockedError(f"IB_ACCOUNT={settings.account} not among connected accounts {account_ids}.")


def assert_can_trade(settings: Settings, account_ids: list[str]) -> None:
    if settings.kill_switch_file.exists():
        raise OrderBlockedError(f"Kill switch active ({settings.kill_switch_file.name} exists).")
    if settings.readonly:
        raise OrderBlockedError("IB_READONLY=true: order sending is disabled.")
    if settings.mode is TradingMode.LIVE and not settings.allow_live_orders:
        raise OrderBlockedError("Live mode requires IB_ALLOW_LIVE_ORDERS=true.")
    assert_account_matches_mode(settings, account_ids)


def assert_within_limits(settings: Settings, quantity: int, max_cost: float) -> None:
    if quantity < 1:
        raise OrderBlockedError("Quantity must be at least 1.")
    if quantity > settings.max_contracts_per_order:
        raise OrderBlockedError(
            f"Quantity {quantity} exceeds IB_MAX_CONTRACTS_PER_ORDER={settings.max_contracts_per_order}."
        )
    if max_cost > settings.max_debit_per_order:
        raise OrderBlockedError(
            f"Max cost {max_cost:,.2f} exceeds IB_MAX_DEBIT_PER_ORDER={settings.max_debit_per_order:,.2f}."
        )
