import pytest
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
