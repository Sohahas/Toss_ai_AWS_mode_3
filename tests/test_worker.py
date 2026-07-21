import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import app.db as db_module
from sqlalchemy import delete

from app.db import AuditLog, PaperCash, PaperHolding, SessionLocal, TradeLog, get_state, init_db
from app.engine import TradingEngine


@pytest.mark.asyncio
async def test_market_poll_updates_shared_dashboard_state():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(PaperHolding))
        await session.execute(delete(PaperCash))
        await session.execute(delete(TradeLog))
        await session.execute(
            delete(AuditLog).where(AuditLog.event_type == "PAPER_PORTFOLIO_REBUILT")
        )
        await session.commit()

    trading_engine = TradingEngine()
    try:
        await trading_engine.poll_market_data()
        async with SessionLocal() as session:
            state = await get_state(session)
            assert state.latest_account is not None
            assert state.latest_account["cash_krw"] == "10000000.0"
            assert state.last_market_poll_at is not None
            assert set(state.market_open) == {"KR", "US"}
    finally:
        await trading_engine.close()


def test_price_action_signal_uses_saved_minute_prices():
    now = datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc)
    history = [
        {
            "captured_at": (now - timedelta(minutes=15)).isoformat(),
            "prices": {"NVDA": "200"},
        },
        {
            "captured_at": (now - timedelta(minutes=5)).isoformat(),
            "prices": {"NVDA": "202"},
        },
    ]
    signals = TradingEngine._price_action_signals(
        history, {"NVDA": Decimal("204")}, captured_at=now
    )
    assert signals["NVDA"]["change_5m_pct"] == "0.990"
    assert signals["NVDA"]["change_15m_pct"] == "2.000"


@pytest.mark.asyncio
async def test_database_startup_retries_after_temporary_dns_failure(monkeypatch):
    calls = 0

    async def flaky_init():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("Temporary failure in name resolution")

    sleep = AsyncMock()
    dispose = AsyncMock()
    monkeypatch.setattr(db_module, "init_db", flaky_init)
    monkeypatch.setattr(db_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(db_module.engine, "dispose", dispose)

    await db_module.init_db_with_retry(max_attempts=4, initial_delay_seconds=0.1)

    assert calls == 3
    assert sleep.await_count == 2
    assert dispose.await_count == 2
