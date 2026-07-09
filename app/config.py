from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "AI 주식 투자 비서"
    environment: Literal["development", "production", "test"] = "development"
    database_url: str = "sqlite+aiosqlite:///./data/assistant.db"
    log_level: str = "INFO"

    dashboard_username: str = "admin"
    dashboard_password: SecretStr = SecretStr("change-me")

    broker_mode: Literal["paper", "toss"] = "paper"
    broker_api_enabled: bool = True
    live_trading_enabled: bool = False
    toss_base_url: str = "https://openapi.tossinvest.com"
    toss_client_id: str | None = None
    toss_client_secret: SecretStr | None = None
    toss_account_seq: int | None = None

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.4-mini"
    market_poll_interval_seconds: int = Field(default=60, ge=10, le=300)
    analysis_interval_seconds: int = Field(default=1800, ge=60, le=7200)
    extended_hours_enabled_by_default: bool = False
    extended_limit_price_buffer_pct: float = Field(default=0.005, ge=0, le=0.01)
    us_day_market_enabled: bool = False
    universe_kr: str = "005930,000660,035420,005380,068270,105560"
    universe_us: str = "AAPL,MSFT,NVDA,GOOGL,AMZN,META,BRK.B"

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    min_confidence: float = Field(default=0.78, ge=0.5, le=1)
    max_position_weight: float = Field(default=0.15, gt=0, le=0.5)
    max_order_weight: float = Field(default=0.05, gt=0, le=0.25)
    min_cash_reserve: float = Field(default=0.20, ge=0, le=0.9)
    max_daily_loss: float = Field(default=0.03, gt=0, le=0.2)
    max_daily_orders: int = Field(default=8, ge=1, le=100)
    max_consecutive_failures: int = Field(default=3, ge=1, le=10)

    paper_cash_krw: float = 10_000_000
    paper_cash_usd: float = 10_000

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            value = value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://") and "+asyncpg" not in value:
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql+asyncpg://"):
            parts = urlsplit(value)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            sslmode = query.pop("sslmode", None)
            if sslmode in {"require", "verify-ca", "verify-full"}:
                query.setdefault("ssl", "true")
            value = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(query),
                    parts.fragment,
                )
            )
        return value

    @field_validator(
        "toss_client_id",
        "toss_client_secret",
        "openai_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
        mode="before",
    )
    @classmethod
    def empty_optional_value_to_none(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("toss_account_seq", mode="before")
    @classmethod
    def empty_account_seq_to_none(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @property
    def kr_symbols(self) -> list[str]:
        return [item.strip().upper() for item in self.universe_kr.split(",") if item.strip()]

    @property
    def us_symbols(self) -> list[str]:
        return [item.strip().upper() for item in self.universe_us.split(",") if item.strip()]

    @model_validator(mode="after")
    def validate_live_configuration(self) -> "Settings":
        if self.broker_mode == "toss" and self.broker_api_enabled:
            secret = (
                self.toss_client_secret.get_secret_value()
                if self.toss_client_secret is not None
                else ""
            )
            if not self.toss_client_id or not secret:
                raise ValueError("BROKER_MODE=toss에는 TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET이 필요합니다.")
        if self.live_trading_enabled and self.broker_mode != "toss":
            raise ValueError("LIVE_TRADING_ENABLED=true에는 BROKER_MODE=toss가 필요합니다.")
        if self.environment == "production" and self.dashboard_password.get_secret_value() == "change-me":
            raise ValueError("운영 환경에서는 DASHBOARD_PASSWORD를 반드시 변경해야 합니다.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
