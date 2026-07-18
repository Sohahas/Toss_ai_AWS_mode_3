from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.broker import TossBroker
from app.config import Settings
from app.db import OrderIntent, SessionLocal, TradeLog, init_db
from app.engine import TradingEngine
from app.schemas import BrokerOrder, Market


def toss_settings() -> Settings:
    return Settings(
        _env_file=None,
        broker_mode="toss",
        toss_client_id="test-client",
        toss_client_secret="test-secret",
    )


def test_order_detail_parses_execution_fields():
    order = TossBroker._parse_order(
        {
            "orderId": "order-1",
            "clientOrderId": "client-1",
            "symbol": "AAPL",
            "side": "BUY",
            "orderType": "MARKET",
            "status": "PARTIAL_FILLED",
            "quantity": "5",
            "orderedAt": "2026-07-19T09:30:00+09:00",
            "execution": {
                "filledQuantity": "3",
                "averageFilledPrice": "185.25",
                "filledAmount": "555.75",
                "commission": "0.99",
                "tax": "0",
            },
        }
    )
    assert order.status == "PARTIAL_FILLED"
    assert order.filled_quantity == Decimal("3")
    assert order.average_filled_price == Decimal("185.25")
    assert order.commission == Decimal("0.99")


@pytest.mark.asyncio
async def test_oco_payload_and_cancel_method(monkeypatch):
    broker = TossBroker(toss_settings())
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/conditional-orders":
            return {"result": {"conditionalOrderId": "oco-1", "clientOrderId": "client-oco"}}
        return {}

    monkeypatch.setattr(broker, "_request", fake_request)
    try:
        result = await broker.create_oco_order(
            symbol="005930",
            quantity=Decimal("2"),
            client_order_id="client-oco",
            take_profit_price=Decimal("310000"),
            stop_trigger_price=Decimal("270000"),
            stop_order_price=Decimal("269500"),
            expire_date="2026-08-18",
        )
        await broker.cancel_conditional_order("oco-1")
    finally:
        await broker.close()

    assert result["conditionalOrderId"] == "oco-1"
    create = calls[0]
    assert create[0:2] == ("POST", "/api/v1/conditional-orders")
    assert create[2]["json"]["type"] == "OCO"
    assert create[2]["json"]["first"]["orderSide"] == "SELL"
    assert create[2]["json"]["second"]["orderSide"] == "SELL"
    assert calls[1][0:2] == ("DELETE", "/api/v1/conditional-orders/oco-1")


def test_price_tick_rounding():
    assert TradingEngine._price_on_tick(Decimal("286777"), Market.KR) == Decimal("286500")
    assert TradingEngine._price_on_tick(Decimal("203.199"), Market.US) == Decimal("203.19")


@pytest.mark.asyncio
async def test_reconcile_filled_order_and_cancel_stale_pending():
    await init_db()

    class FakeBroker:
        def __init__(self):
            self.canceled = []

        async def find_order(self, client_order_id, symbol):
            return BrokerOrder(
                order_id="unit-filled-order",
                client_order_id=client_order_id,
                symbol=symbol,
                side="BUY",
                status="FILLED",
                order_type="MARKET",
                quantity=Decimal("1"),
                filled_quantity=Decimal("1"),
                average_filled_price=Decimal("100"),
            )

        async def order_detail(self, order_id):
            return BrokerOrder(
                order_id=order_id,
                client_order_id="unit-stale-client",
                symbol="MSFT",
                side="BUY",
                status="PENDING",
                order_type="LIMIT",
                quantity=Decimal("1"),
            )

        async def cancel_order(self, order_id):
            self.canceled.append(order_id)

    async with SessionLocal() as session:
        await session.execute(
            delete(OrderIntent).where(OrderIntent.client_order_id.like("unit-%"))
        )
        await session.execute(
            delete(TradeLog).where(TradeLog.order_id.like("unit-%"))
        )
        session.add_all(
            [
                OrderIntent(
                    client_order_id="unit-filled-client",
                    market="US",
                    symbol="AAPL",
                    side="BUY",
                    quantity="1",
                    order_type="MARKET",
                    status="PREPARED",
                    raw={"protect_with_oco": False, "stock_name": "Apple"},
                ),
                OrderIntent(
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    client_order_id="unit-stale-client",
                    order_id="unit-stale-order",
                    market="US",
                    symbol="MSFT",
                    side="BUY",
                    quantity="1",
                    order_type="LIMIT",
                    price="100",
                    status="PENDING",
                    raw={"protect_with_oco": False, "stock_name": "Microsoft"},
                ),
            ]
        )
        await session.commit()

    settings = toss_settings()
    settings.pending_order_timeout_seconds = 300
    engine = TradingEngine(settings)
    fake = FakeBroker()
    real_broker = engine.broker
    engine.broker = fake
    try:
        await engine._reconcile_orders({"AAPL": Decimal("100"), "MSFT": Decimal("100")})
    finally:
        await real_broker.close()

    async with SessionLocal() as session:
        filled = await session.scalar(
            select(OrderIntent).where(OrderIntent.client_order_id == "unit-filled-client")
        )
        stale = await session.scalar(
            select(OrderIntent).where(OrderIntent.client_order_id == "unit-stale-client")
        )
        trade = await session.scalar(
            select(TradeLog).where(TradeLog.order_id == "unit-filled-order")
        )
        assert filled.status == "FILLED"
        assert trade.status == "FILLED"
        assert stale.status == "PENDING_CANCEL"
        assert fake.canceled == ["unit-stale-order"]
