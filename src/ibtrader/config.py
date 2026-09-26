"""Runtime settings, loaded from environment variables / .env (never from committed files)."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PAPER_PORTS = {4002, 7497}  # IB Gateway paper, TWS paper
LIVE_PORTS = {4001, 7496}  # IB Gateway live, TWS live
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_prefix="IB_", extra="ignore", env_file_encoding="utf-8"
    )

    mode: TradingMode = TradingMode.PAPER
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    account: str | None = None

    readonly: bool = True
    allow_live_orders: bool = False
    allow_remote_host: bool = False

    max_contracts_per_order: int = 5
    max_debit_per_order: float = 2000.0

    kill_switch_file: Path = PROJECT_ROOT / "KILL_SWITCH"

    # Dashboard (always connects read-only, never sends orders)
    dashboard_client_id: int = 11
    dashboard_port: int = 8050
    market_data_type: int = 3  # 1=live, 3=delayed (returns live where you have a subscription)
    watchlist_file: Path = PROJECT_ROOT / "config" / "watchlist.yaml"

    # Optional Flex Web Service for full trade/cost history (Client Portal > Reports > Flex Queries)
    flex_token: SecretStr | None = None
    flex_query_id: str | None = None
    data_dir: Path = PROJECT_ROOT / "data"

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.account == "":
            self.account = None
        if self.flex_query_id == "":
            self.flex_query_id = None
        if self.flex_token is not None and not self.flex_token.get_secret_value():
            self.flex_token = None
        if self.host not in LOCAL_HOSTS and not self.allow_remote_host:
            raise ValueError(
                f"IB_HOST={self.host!r} is not local. The API socket is unencrypted; "
                "only connect to 127.0.0.1 (or set IB_ALLOW_REMOTE_HOST=true behind a VPN/SSH tunnel)."
            )
        if self.mode is TradingMode.PAPER and self.port in LIVE_PORTS:
            raise ValueError(f"IB_MODE=paper but IB_PORT={self.port} is a LIVE port.")
        if self.mode is TradingMode.LIVE and self.port in PAPER_PORTS:
            raise ValueError(f"IB_MODE=live but IB_PORT={self.port} is a paper port.")
        if self.max_contracts_per_order < 1 or self.max_debit_per_order <= 0:
            raise ValueError("Order limits must be positive.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
