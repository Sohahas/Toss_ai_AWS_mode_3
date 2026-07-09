from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.broker import PaperBroker
from app.config import Settings
from app.db import AuditLog, PaperCash, PaperHolding, SessionLocal, TradeLog, init_db
from app.schemas import Action, Market, OrderRequest


@pytest.mark.asyncio
async def test_paper_buy_and_sell_updates_cash_and_holdings():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(PaperHolding))
        await session.execute(delete(PaperCash))
        await session.commit()

    broker = PaperBroker(Settings(_env_file=None))

    before = await broker.account_snapshot()
    assert before.cash_krw == Decimal("10000000.0")
    assert before.holdings == []

    await broker.place_order(
        OrderRequest(
            symbol="000660",
            market=Market.KR,
            action=Action.BUY,
            quantity=Decimal("2"),
            order_type="MARKET",
            client_order_id="test-buy-000660",
        )
    )

    after_buy = await broker.account_snapshot()
    assert after_buy.cash_krw == Decimal("9580000.0")
    assert len(after_buy.holdings) == 1
    assert after_buy.holdings[0].symbol == "000660"
    assert after_buy.holdings[0].name == "SK하이닉스"
    assert after_buy.holdings[0].quantity == Decimal("2")

    await broker.place_order(
        OrderRequest(
            symbol="000660",
            market=Market.KR,
            action=Action.SELL,
            quantity=Decimal("2"),
            order_type="MARKET",
            client_order_id="test-sell-000660",
        )
    )

    after_sell = await broker.account_snapshot()
    assert after_sell.cash_krw == Decimal("10000000.0")
    assert after_sell.holdings == []


@pytest.mark.asyncio
async def test_old_paper_trade_logs_are_rebuilt_into_cash_and_holdings():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(PaperHolding))
        await session.execute(delete(PaperCash))
        await session.execute(delete(TradeLog))
        await session.execute(
            delete(AuditLog).where(AuditLog.event_type == "PAPER_PORTFOLIO_REBUILT")
        )
        session.add(
            TradeLog(
                market="KR",
                symbol="000660",
                side="BUY",
                quantity="2",
                price="210000",
                order_id="old-paper-kr-buy",
                status="PAPER_FILLED",
                rationale="구버전 모의 매수 기록",
                raw={},
            )
        )
        session.add(
            TradeLog(
                market="US",
                symbol="MSFT",
                side="BUY",
                quantity="1",
                price="480",
                order_id="old-paper-us-buy",
                status="PAPER_FILLED",
                rationale="구버전 모의 매수 기록",
                raw={},
            )
        )
        await session.commit()

    broker = PaperBroker(Settings(_env_file=None))
    snapshot = await broker.account_snapshot()

    assert snapshot.cash_krw == Decimal("9580000.0")
    assert snapshot.cash_usd == Decimal("9520.0")
    assert snapshot.equity_krw == Decimal("10000000.0")
    assert snapshot.equity_usd == Decimal("10000.0")
    assert {(holding.symbol, holding.quantity) for holding in snapshot.holdings} == {
        ("000660", Decimal("2")),
        ("MSFT", Decimal("1")),
    }
