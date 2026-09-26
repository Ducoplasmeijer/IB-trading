"""Connection to a locally running IB Gateway / TWS."""

from collections.abc import Iterator
from contextlib import contextmanager

from ib_async import IB

from .config import Settings, get_settings
from .safety import assert_account_matches_mode


@contextmanager
def connect(settings: Settings | None = None, client_id: int | None = None) -> Iterator[IB]:
    """Connect, verify the account matches the configured mode, and always disconnect."""
    settings = settings or get_settings()
    ib = IB()
    ib.connect(
        settings.host,
        settings.port,
        clientId=client_id if client_id is not None else settings.client_id,
        readonly=settings.readonly,
        account=settings.account or "",
        timeout=15,
    )
    try:
        assert_account_matches_mode(settings, ib.managedAccounts())
        yield ib
    finally:
        ib.disconnect()
