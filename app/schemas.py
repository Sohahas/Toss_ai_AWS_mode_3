from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Market(StrEnum):
    KR = "KR"
    US = "US"


class MarketSession(StrEnum):
    CLOSED = "CLOSED"
    DAY = "DAY"
    PRE = "PRE"
    REGULAR = "REGULAR"
    AFTER = "AFTER"


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Evidence(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    url: str | None = Field(default=None, max_length=500)
    published_at: str | None = None
    fact: str = Field(min_length=3, max_length=600)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        clean = value.strip()
        if not clean.startswith(("http://", "https://")):
            raise ValueError("URL은 http:// 또는 https://로 시작해야 합니다.")
        return clean


class TradeProposal(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9.\-]{1,16}$")
    market: Market
    action: Action
    confidence: float = Field(ge=0, le=1)
    target_weight_pct: float = Field(ge=0, le=100)
    expected_return_pct: float = Field(ge=-100, le=500)
    risk_score: int = Field(ge=1, le=10)
    thesis: str = Field(min_length=10, max_length=1800)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)


class DiscoveryCandidate(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9.\-]{1,16}$")
    market: Market
    rationale: str = Field(min_length=10, max_length=800)
    evidence: list[Evidence] = Field(default_factory=list, min_length=2, max_length=6)


class DiscoveryResult(BaseModel):
    candidates: list[DiscoveryCandidate] = Field(default_factory=list, max_length=8)


class ResearchDecision(BaseModel):
    market_regime: str = Field(min_length=3, max_length=80)
    market_summary: str = Field(min_length=10, max_length=1200)
    proposals: list[TradeProposal] = Field(default_factory=list, max_length=12)


class Holding(BaseModel):
    symbol: str
    name: str
    market: Market
    currency: str
    quantity: Decimal
    last_price: Decimal
    average_price: Decimal
    market_value: Decimal
    profit_loss: Decimal
    profit_rate: Decimal


class StockInfo(BaseModel):
    symbol: str
    name: str
    market_name: str
    security_type: str
    status: str
    currency: str
    leverage_factor: Decimal | None = None
    liquidation_trading: bool = False
    nxt_supported: bool = False
    krx_trading_suspended: bool = False
    nxt_trading_suspended: bool | None = None


class AccountSnapshot(BaseModel):
    captured_at: datetime
    holdings: list[Holding]
    cash_krw: Decimal
    cash_usd: Decimal
    equity_krw: Decimal
    equity_usd: Decimal
    total_profit_rate: Decimal = Decimal("0")
    daily_return: Decimal = Decimal("0")


class OrderRequest(BaseModel):
    symbol: str
    market: Market
    action: Action
    quantity: Decimal | None = Field(default=None, gt=0)
    order_amount: Decimal | None = Field(default=None, gt=0)
    order_type: str = "MARKET"
    price: Decimal | None = None
    market_session: MarketSession = MarketSession.REGULAR
    time_in_force: str = "DAY"
    client_order_id: str


class OrderResult(BaseModel):
    order_id: str
    client_order_id: str
    status: str
    raw: dict = Field(default_factory=dict)


class RiskContext(BaseModel):
    market_open: bool
    market_session: MarketSession = MarketSession.REGULAR
    extended_hours_enabled: bool = False
    order_type: str = "MARKET"
    stock: StockInfo
    warnings: list[str]
    buying_power: Decimal
    sellable_quantity: Decimal
    current_quantity: Decimal
    current_position_value: Decimal
    portfolio_equity: Decimal
    daily_return: Decimal
    daily_order_count: int
    proposed_quantity: Decimal
    proposed_notional: Decimal


class RiskResult(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
