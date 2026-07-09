from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings
from app.profiles import DEFAULT_PROFILE, normalize_profile_key


class Base(DeclarativeBase):
    pass


class SystemState(Base):
    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active_broker_mode: Mapped[str] = mapped_column(String(16), default="paper")
    trading_profile: Mapped[str] = mapped_column(String(32), default=DEFAULT_PROFILE, nullable=False)
    extended_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trading_armed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    circuit_breaker: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    breaker_reason: Mapped[str | None] = mapped_column(Text)
    current_strategy: Mapped[str] = mapped_column(String(120), default="데이터 수집 대기")
    market_view: Mapped[str] = mapped_column(Text, default="분석 전")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    discovered_symbols: Mapped[list] = mapped_column(JSON, default=list)
    latest_prices: Mapped[dict] = mapped_column(JSON, default=dict)
    latest_account: Mapped[dict | None] = mapped_column(JSON)
    market_open: Mapped[dict] = mapped_column(JSON, default=dict)
    market_sessions: Mapped[dict] = mapped_column(JSON, default=dict)
    last_market_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=func.now()
    )


class DecisionLog(Base):
    __tablename__ = "decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    market: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    action: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    thesis: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    expected_return_pct: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    rejection_reasons: Mapped[list] = mapped_column(JSON, default=list)
    order_id: Mapped[str | None] = mapped_column(String(160))


class TradeLog(Base):
    __tablename__ = "trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    source: Mapped[str] = mapped_column(String(16), default="AI")
    market: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[str] = mapped_column(String(40))
    price: Mapped[str | None] = mapped_column(String(40))
    order_id: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(40))
    rationale: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    broker_mode: Mapped[str] = mapped_column(String(16), default="paper", index=True)
    equity_krw: Mapped[str] = mapped_column(String(40))
    equity_usd: Mapped[str] = mapped_column(String(40))
    cash_krw: Mapped[str] = mapped_column(String(40))
    cash_usd: Mapped[str] = mapped_column(String(40))
    total_profit_rate: Mapped[float] = mapped_column(Float, default=0)
    daily_return: Mapped[float] = mapped_column(Float, default=0)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class PaperCash(Base):
    __tablename__ = "paper_cash"

    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    amount: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=func.now()
    )


class PaperHolding(Base):
    __tablename__ = "paper_holdings"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    market: Mapped[str] = mapped_column(String(8))
    currency: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[str] = mapped_column(String(40))
    average_price: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


settings = get_settings()
if settings.database_url.startswith("sqlite") and ":memory:" not in settings.database_url:
    sqlite_path = settings.database_url.split("///", 1)[-1]
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def _run_lightweight_migrations(connection) -> None:
    """기존 DB를 그대로 쓰는 사용자를 위한 최소 자동 보강.

    SQLAlchemy의 create_all()은 새 테이블은 만들지만, 이미 존재하는 테이블에
    새 컬럼을 추가하지는 않습니다. 배포 후 업데이트하는 사용자가 대시보드를
    다시 켰을 때 깨지지 않도록 system_state 컬럼만 안전하게 보강합니다.
    """

    def migrate(sync_connection) -> None:
        inspector = inspect(sync_connection)
        if "system_state" not in inspector.get_table_names():
            return

        columns = {column["name"] for column in inspector.get_columns("system_state")}
        dialect = sync_connection.dialect.name

        def add_column(name: str, sqlite_sql: str, postgres_sql: str | None = None) -> None:
            if name in columns:
                return
            sql = postgres_sql if dialect.startswith("postgresql") and postgres_sql else sqlite_sql
            sync_connection.exec_driver_sql(sql)

        add_column(
            "active_broker_mode",
            "ALTER TABLE system_state ADD COLUMN active_broker_mode VARCHAR(16) DEFAULT 'paper'",
        )
        add_column(
            "trading_profile",
            f"ALTER TABLE system_state ADD COLUMN trading_profile VARCHAR(32) DEFAULT '{DEFAULT_PROFILE}' NOT NULL",
        )
        add_column(
            "extended_hours_enabled",
            "ALTER TABLE system_state ADD COLUMN extended_hours_enabled BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE system_state ADD COLUMN extended_hours_enabled BOOLEAN DEFAULT false NOT NULL",
        )
        add_column(
            "trading_armed",
            "ALTER TABLE system_state ADD COLUMN trading_armed BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE system_state ADD COLUMN trading_armed BOOLEAN DEFAULT false NOT NULL",
        )
        add_column(
            "circuit_breaker",
            "ALTER TABLE system_state ADD COLUMN circuit_breaker BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE system_state ADD COLUMN circuit_breaker BOOLEAN DEFAULT false NOT NULL",
        )
        add_column("breaker_reason", "ALTER TABLE system_state ADD COLUMN breaker_reason TEXT")
        add_column(
            "current_strategy",
            "ALTER TABLE system_state ADD COLUMN current_strategy VARCHAR(120) DEFAULT '데이터 수집 대기'",
        )
        add_column("market_view", "ALTER TABLE system_state ADD COLUMN market_view TEXT DEFAULT '분석 전'")
        add_column(
            "consecutive_failures",
            "ALTER TABLE system_state ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
        )
        add_column(
            "discovered_symbols",
            "ALTER TABLE system_state ADD COLUMN discovered_symbols JSON DEFAULT '[]'",
            "ALTER TABLE system_state ADD COLUMN discovered_symbols JSON DEFAULT '[]'::json",
        )
        add_column(
            "latest_prices",
            "ALTER TABLE system_state ADD COLUMN latest_prices JSON DEFAULT '{}'",
            "ALTER TABLE system_state ADD COLUMN latest_prices JSON DEFAULT '{}'::json",
        )
        add_column("latest_account", "ALTER TABLE system_state ADD COLUMN latest_account JSON")
        add_column(
            "market_open",
            "ALTER TABLE system_state ADD COLUMN market_open JSON DEFAULT '{}'",
            "ALTER TABLE system_state ADD COLUMN market_open JSON DEFAULT '{}'::json",
        )
        add_column(
            "market_sessions",
            "ALTER TABLE system_state ADD COLUMN market_sessions JSON DEFAULT '{}'",
            "ALTER TABLE system_state ADD COLUMN market_sessions JSON DEFAULT '{}'::json",
        )
        add_column(
            "last_market_poll_at",
            "ALTER TABLE system_state ADD COLUMN last_market_poll_at DATETIME",
            "ALTER TABLE system_state ADD COLUMN last_market_poll_at TIMESTAMP WITH TIME ZONE",
        )
        add_column(
            "last_cycle_at",
            "ALTER TABLE system_state ADD COLUMN last_cycle_at DATETIME",
            "ALTER TABLE system_state ADD COLUMN last_cycle_at TIMESTAMP WITH TIME ZONE",
        )
        add_column(
            "updated_at",
            "ALTER TABLE system_state ADD COLUMN updated_at DATETIME",
            "ALTER TABLE system_state ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE",
        )

    await connection.run_sync(migrate)


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await _run_lightweight_migrations(connection)
    async with SessionLocal() as session:
        state = await session.get(SystemState, 1)
        if state is None:
            session.add(
                SystemState(
                    id=1,
                    active_broker_mode=settings.broker_mode,
                    trading_profile=DEFAULT_PROFILE,
                    extended_hours_enabled=settings.extended_hours_enabled_by_default,
                    trading_armed=settings.broker_mode == "paper",
                )
            )
            await session.commit()
        elif state.active_broker_mode != settings.broker_mode:
            state.active_broker_mode = settings.broker_mode
            state.trading_armed = settings.broker_mode == "paper"
            state.circuit_breaker = False
            state.breaker_reason = None
            state.latest_account = None
            state.latest_prices = {}
            state.market_open = {}
            state.market_sessions = {}
            state.last_market_poll_at = None
            state.trading_profile = normalize_profile_key(state.trading_profile)
            state.current_strategy = "브로커 모드 전환 후 계좌 재조회 대기"
            state.market_view = "브로커 모드가 변경되어 이전 계좌 화면 값을 지웠습니다. 새 계좌 정보를 다시 조회합니다."
            await session.commit()
        elif state.trading_profile != normalize_profile_key(state.trading_profile):
            state.trading_profile = normalize_profile_key(state.trading_profile)
            await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def audit(
    session: AsyncSession,
    event_type: str,
    message: str,
    *,
    level: str = "INFO",
    details: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            event_type=event_type,
            message=message,
            level=level,
            details=details or {},
        )
    )


def add_portfolio_snapshot(session: AsyncSession, snapshot, broker_mode: str) -> None:
    session.add(
        PortfolioSnapshot(
            broker_mode=broker_mode,
            equity_krw=str(snapshot.equity_krw),
            equity_usd=str(snapshot.equity_usd),
            cash_krw=str(snapshot.cash_krw),
            cash_usd=str(snapshot.cash_usd),
            total_profit_rate=float(snapshot.total_profit_rate),
            daily_return=float(snapshot.daily_return),
            raw=snapshot.model_dump(mode="json"),
        )
    )


async def get_state(session: AsyncSession) -> SystemState:
    state = await session.scalar(select(SystemState).where(SystemState.id == 1))
    if state is None:
        state = SystemState(id=1, trading_profile=DEFAULT_PROFILE)
        session.add(state)
        await session.flush()
    elif state.trading_profile != normalize_profile_key(state.trading_profile):
        state.trading_profile = normalize_profile_key(state.trading_profile)
    return state
