import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from sqlalchemy import func, select

from app.ai import InvestmentAI
from app.broker import (
    Broker,
    BrokerError,
    BrokerMaintenanceError,
    BrokerTransportError,
    create_broker,
    friendly_error_message,
)
from app.config import Settings, get_settings
from app.db import (
    AuditLog,
    DecisionLog,
    OrderIntent,
    ProtectionOrder,
    SessionLocal,
    TradeLog,
    add_portfolio_snapshot,
    audit,
    get_state,
)
from app.notifier import TelegramNotifier
from app.profiles import DEFAULT_PROFILE, get_profile, limits_for_profile, profile_ai_context
from app.risk import RiskManager
from app.schemas import Action, BrokerOrder, Market, MarketSession, OrderRequest, RiskContext, TradeProposal

logger = logging.getLogger(__name__)

ACTIVE_ORDER_STATUSES = {
    "PREPARED",
    "AMBIGUOUS",
    "SUBMITTED",
    "PENDING",
    "PARTIAL_FILLED",
    "PENDING_CANCEL",
    "PENDING_REPLACE",
}
TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "REPLACED", "PAPER_FILLED"}
ACTIVE_PROTECTION_STATUSES = {
    "PREPARING",
    "UNCERTAIN",
    "WATCHING",
    "PAUSED",
    "ORDERING",
    "ORDERED",
}

WARNING_LABELS = {
    "LIQUIDATION_TRADING": "정리매매",
    "OVERHEATED": "단기과열종목",
    "INVESTMENT_WARNING": "투자경고종목",
    "INVESTMENT_RISK": "투자위험종목",
    "VI_STATIC_AND_DYNAMIC": "변동성 완화장치(VI) 정적·동적 동시 발동",
    "VI_STATIC": "변동성 완화장치(VI) 정적 발동",
    "VI_DYNAMIC": "변동성 완화장치(VI) 동적 발동",
    "STOCK_WARRANTS": "신주인수권증서/증권",
}


class TradingEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.broker: Broker = create_broker(self.settings)
        self.ai = InvestmentAI(self.settings)
        self.risk = RiskManager(self.settings)
        self.notifier = TelegramNotifier(self.settings)
        self._aws_reconnect_pending = True
        self._toss_api_disconnected_at: datetime | None = None
        self._toss_maintenance_active = False
        self._toss_maintenance_started_at: datetime | None = None
        self._worker_started_at = datetime.now(timezone.utc)
        self._heartbeat_initialized = False

    async def close(self) -> None:
        await self.broker.close()

    @staticmethod
    def _aware_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    async def record_heartbeat(self) -> None:
        """브로커 장애와 별개로 AWS 워커 프로세스가 살아 있음을 DB에 남긴다."""
        try:
            async with SessionLocal() as session:
                state = await get_state(session)
                state.worker_heartbeat_at = datetime.now(timezone.utc)
                if not self._heartbeat_initialized:
                    state.worker_started_at = self._worker_started_at
                await session.commit()
                self._heartbeat_initialized = True
        except Exception:
            logger.exception("워커 심박 기록 실패")

    def _account_payload(self, snapshot) -> dict:
        payload = snapshot.model_dump(mode="json")
        payload["_broker_mode"] = self.settings.broker_mode
        payload["_captured_by"] = "worker"
        return payload

    def _is_extended_session(self, session: MarketSession) -> bool:
        return session in {MarketSession.DAY, MarketSession.PRE, MarketSession.AFTER}

    def _trading_enabled_for_session(
        self,
        market: Market,
        session: MarketSession,
        extended_hours_enabled: bool,
        day_market_enabled: bool = False,
        trading_profile: str | None = None,
    ) -> bool:
        if session == MarketSession.REGULAR:
            return True
        if session == MarketSession.DAY:
            return (
                market == Market.US
                and extended_hours_enabled
                and day_market_enabled
                and trading_profile in {"aggressive", "max_return"}
            )
        if session in {MarketSession.PRE, MarketSession.AFTER}:
            return extended_hours_enabled
        return False

    def _session_payload(
        self,
        market: Market,
        session: MarketSession,
        extended_hours_enabled: bool,
        day_market_enabled: bool = False,
        trading_profile: str | None = None,
    ) -> dict:
        labels = {
            MarketSession.CLOSED: "장 종료",
            MarketSession.DAY: "데이마켓",
            MarketSession.PRE: "프리마켓",
            MarketSession.REGULAR: "정규장",
            MarketSession.AFTER: "애프터마켓",
        }
        return {
            "market": market.value,
            "session": session.value,
            "label": labels[session],
            "is_open": session != MarketSession.CLOSED,
            "is_extended": self._is_extended_session(session),
            "trading_enabled": self._trading_enabled_for_session(
                market, session, extended_hours_enabled, day_market_enabled, trading_profile
            ),
            "day_market_profile_required": session == MarketSession.DAY,
            "day_market_profile_allowed": trading_profile in {"aggressive", "max_return"},
        }

    async def _audit_market_availability_changes(
        self,
        session,
        previous: dict | None,
        current: dict,
    ) -> None:
        previous = previous or {}
        market_labels = {"KR": "국내", "US": "미국"}
        for market_code in ("KR", "US"):
            before = previous.get(market_code) or {}
            after = current.get(market_code) or {}
            before_signature = (before.get("session"), before.get("trading_enabled"))
            after_signature = (after.get("session"), after.get("trading_enabled"))
            if before_signature == after_signature:
                continue

            market_label = market_labels[market_code]
            session_label = after.get("label") or "시장 상태 확인 중"
            if after.get("trading_enabled"):
                message = f"{market_label} {session_label}: 현재 매수·매도 가능합니다."
                level = "INFO"
            elif after.get("is_open"):
                message = (
                    f"{market_label} {session_label}: 장은 열렸지만 현재 설정상 "
                    "매수·매도할 수 없습니다."
                )
                level = "WARNING"
            else:
                message = f"{market_label} 장 종료: 현재 매수·매도할 수 없습니다."
                level = "INFO"
            await audit(
                session,
                "TRADING_AVAILABILITY",
                message,
                level=level,
                details={
                    "market": market_code,
                    "session": after.get("session"),
                    "trading_enabled": bool(after.get("trading_enabled")),
                },
            )

    async def _audit_special_status(
        self,
        session,
        *,
        symbol: str,
        name: str,
        labels: list[str],
        codes: list[str],
    ) -> None:
        if not labels:
            return
        message = f"{name}({symbol}) 거래 특이사항 감지: {', '.join(labels)}"
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        duplicate = await session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.event_type == "MARKET_SPECIAL_STATUS",
                AuditLog.message == message,
                AuditLog.created_at >= since,
            )
            .limit(1)
        )
        if duplicate:
            return
        await audit(
            session,
            "MARKET_SPECIAL_STATUS",
            message,
            level="WARNING",
            details={"symbol": symbol, "name": name, "codes": codes},
        )

    async def _record_stock_reference_statuses(self, stocks: dict) -> None:
        async with SessionLocal() as session:
            for symbol, stock in stocks.items():
                labels: list[str] = []
                codes: list[str] = []
                if stock.status != "ACTIVE":
                    labels.append(f"상장 상태 {stock.status}")
                    codes.append(f"STATUS_{stock.status}")
                if stock.liquidation_trading:
                    labels.append("정리매매")
                    codes.append("LIQUIDATION_TRADING")
                if stock.krx_trading_suspended:
                    labels.append("KRX 거래정지")
                    codes.append("KRX_TRADING_SUSPENDED")
                if stock.nxt_trading_suspended is True:
                    labels.append("NXT 거래정지")
                    codes.append("NXT_TRADING_SUSPENDED")
                await self._audit_special_status(
                    session,
                    symbol=symbol,
                    name=stock.name,
                    labels=labels,
                    codes=codes,
                )
            await session.commit()

    def _limit_price_for_extended_session(
        self,
        price: Decimal,
        market: Market,
        action: Action,
    ) -> Decimal:
        buffer = Decimal(str(self.settings.extended_limit_price_buffer_pct))
        multiplier = Decimal("1") + buffer if action == Action.BUY else Decimal("1") - buffer
        raw_price = price * multiplier
        quant = Decimal("1") if market == Market.KR else Decimal("0.01")
        rounding = ROUND_UP if action == Action.BUY else ROUND_DOWN
        return raw_price.quantize(quant, rounding=rounding)

    def _minimum_order_amount(self, market: Market) -> Decimal:
        if market == Market.KR:
            return Decimal(str(self.settings.min_order_amount_krw))
        return Decimal(str(self.settings.min_order_amount_usd))

    def _minimum_remaining_position_amount(self, market: Market) -> Decimal:
        if market == Market.KR:
            return Decimal(str(self.settings.min_remaining_position_amount_krw))
        return Decimal(str(self.settings.min_remaining_position_amount_usd))

    async def run_cycle(self) -> None:
        try:
            await self._run_cycle()
            await self._record_connection_recovery()
        except BrokerMaintenanceError as exc:
            self._remember_toss_api_failure(exc)
            logger.warning("토스증권 시스템 점검 중: %s", exc)
            await self._record_toss_maintenance()
        except Exception as exc:
            self._remember_toss_api_failure(exc)
            logger.exception("투자 사이클 실패")
            await self._record_failure(friendly_error_message(str(exc)))

    async def poll_market_data(self) -> None:
        """장중 REST 시세를 짧은 주기로 수집해 웹/워커가 공유하는 DB에 저장한다."""
        try:
            sessions = {
                Market.KR: await self.broker.market_session(Market.KR),
                Market.US: await self.broker.market_session(Market.US),
            }
            snapshot = await self.broker.account_snapshot()
            async with SessionLocal() as session:
                state = await get_state(session)
                symbols = {
                    holding.symbol.upper() for holding in snapshot.holdings
                }
                symbols.update(self.settings.kr_symbols)
                symbols.update(self.settings.us_symbols)
                symbols.update(state.discovered_symbols or [])
                any_market_open = any(value != MarketSession.CLOSED for value in sessions.values())
                prices = await self.broker.prices(sorted(symbols)) if any_market_open else {}
                state.active_broker_mode = self.settings.broker_mode
                state.latest_account = self._account_payload(snapshot)
                state.latest_prices = {key: str(value) for key, value in prices.items()}
                next_market_open = {
                    market.value: self._trading_enabled_for_session(
                        market,
                        market_session,
                        state.extended_hours_enabled,
                        state.day_market_enabled,
                        state.trading_profile or DEFAULT_PROFILE,
                    )
                    for market, market_session in sessions.items()
                }
                next_market_sessions = {
                    market.value: self._session_payload(
                        market,
                        market_session,
                        state.extended_hours_enabled,
                        state.day_market_enabled,
                        state.trading_profile or DEFAULT_PROFILE,
                    )
                    for market, market_session in sessions.items()
                }
                await self._audit_market_availability_changes(
                    session, state.market_sessions, next_market_sessions
                )
                state.market_open = next_market_open
                state.market_sessions = next_market_sessions
                state.last_market_poll_at = datetime.now(timezone.utc)
                add_portfolio_snapshot(session, snapshot, self.settings.broker_mode)
                await session.commit()
            await self._reconcile_orders(prices)
            await self._reconcile_protections(snapshot)
            await self._record_connection_recovery()
        except BrokerMaintenanceError as exc:
            self._remember_toss_api_failure(exc)
            logger.warning("토스증권 시스템 점검 중: %s", exc)
            await self._record_toss_maintenance()
        except Exception as exc:
            self._remember_toss_api_failure(exc)
            logger.exception("실시간 시장 데이터 수집 실패")
            await self._record_failure(f"시장 데이터 수집 실패: {friendly_error_message(str(exc))}")

    def _remember_toss_api_failure(self, exc: Exception) -> None:
        if (
            self.settings.broker_mode == "toss"
            and isinstance(exc, BrokerError)
            and self._toss_api_disconnected_at is None
        ):
            self._toss_api_disconnected_at = datetime.now(timezone.utc)

    async def _record_toss_maintenance(self) -> None:
        """점검 시작을 한 번만 기록하고 다음 정상 갱신까지 오류 누적을 멈춘다."""
        if self._toss_maintenance_active:
            return
        now = datetime.now(timezone.utc)
        self._toss_maintenance_active = True
        self._toss_maintenance_started_at = now
        async with SessionLocal() as session:
            await audit(
                session,
                "TOSS_MAINTENANCE",
                "토스증권 시스템 점검 중입니다. 자동 주문을 일시 대기하고 1분마다 재확인합니다.",
                level="WARNING",
                details={"started_at": now.isoformat(), "retry_seconds": 60},
            )
            await session.commit()

    async def _record_connection_recovery(self) -> None:
        """AWS 주문봇 재시작과 토스 API 통신 복구를 정상 응답 후 기록한다."""
        if self.settings.broker_mode != "toss":
            self._aws_reconnect_pending = False
            self._toss_api_disconnected_at = None
            self._toss_maintenance_active = False
            self._toss_maintenance_started_at = None
            return
        if not self._aws_reconnect_pending and self._toss_api_disconnected_at is None:
            return

        now = datetime.now(timezone.utc)
        async with SessionLocal() as session:
            if self._aws_reconnect_pending:
                await audit(
                    session,
                    "AWS_SERVER_RECONNECTED",
                    "AWS 주문봇 서버가 다시 연결되었습니다.",
                    details={"connected_at": now.isoformat()},
                )
                await audit(
                    session,
                    "TOSS_API_RECONNECTED",
                    "토스증권 API 연결이 정상적으로 확인되었습니다.",
                    details={"connected_at": now.isoformat(), "after_server_restart": True},
                )
            elif self._toss_api_disconnected_at is not None:
                interrupted_seconds = max(
                    0, int((now - self._toss_api_disconnected_at).total_seconds())
                )
                if interrupted_seconds < 60:
                    duration_text = f"약 {interrupted_seconds}초"
                else:
                    duration_text = f"약 {max(1, round(interrupted_seconds / 60))}분"
                await audit(
                    session,
                    "TOSS_API_RECONNECTED",
                    f"토스증권 API가 다시 연결되었습니다. 중단 추정 시간: {duration_text}",
                    details={
                        "disconnected_at": self._toss_api_disconnected_at.isoformat(),
                        "connected_at": now.isoformat(),
                        "interrupted_seconds": interrupted_seconds,
                    },
                )
            await session.commit()

        self._aws_reconnect_pending = False
        self._toss_api_disconnected_at = None
        self._toss_maintenance_active = False
        self._toss_maintenance_started_at = None

    @staticmethod
    def _tick_size(price: Decimal, market: Market) -> Decimal:
        if market == Market.US:
            return Decimal("0.01")
        if price < 2_000:
            return Decimal("1")
        if price < 5_000:
            return Decimal("5")
        if price < 20_000:
            return Decimal("10")
        if price < 50_000:
            return Decimal("50")
        if price < 200_000:
            return Decimal("100")
        if price < 500_000:
            return Decimal("500")
        return Decimal("1000")

    @classmethod
    def _price_on_tick(
        cls, price: Decimal, market: Market, *, rounding=ROUND_DOWN
    ) -> Decimal:
        tick = cls._tick_size(price, market)
        return (price / tick).quantize(Decimal("1"), rounding=rounding) * tick

    async def _backfill_order_intents(self, session) -> None:
        """4.0 이전의 접수 주문도 새 체결 동기화 대상으로 편입한다."""
        rows = (
            await session.scalars(
                select(TradeLog).where(TradeLog.status.in_(ACTIVE_ORDER_STATUSES))
            )
        ).all()
        for trade in rows:
            existing = await session.scalar(
                select(OrderIntent.id).where(OrderIntent.order_id == trade.order_id)
            )
            if existing:
                continue
            raw = trade.raw or {}
            result = raw.get("result") or {}
            client_order_id = (
                result.get("clientOrderId")
                or raw.get("client_order_id")
                or f"legacy-{trade.id}-{uuid.uuid4().hex[:12]}"
            )
            session.add(
                OrderIntent(
                    client_order_id=client_order_id,
                    order_id=trade.order_id,
                    market=trade.market,
                    symbol=trade.symbol,
                    side=trade.side,
                    quantity=trade.quantity,
                    order_amount=raw.get("order_amount"),
                    order_type=raw.get("order_type") or "MARKET",
                    price=trade.price,
                    status=trade.status,
                    raw={"legacy": True, "protect_with_oco": False},
                )
            )
        await session.flush()

    @staticmethod
    def _broker_order_payload(order: BrokerOrder) -> dict:
        return order.model_dump(mode="json")

    async def _upsert_trade_from_order(
        self, session, intent: OrderIntent, order: BrokerOrder
    ) -> TradeLog:
        trade = await session.scalar(
            select(TradeLog).where(TradeLog.order_id == order.order_id)
        )
        execution_price = order.average_filled_price or order.price
        quantity = order.filled_quantity if order.filled_quantity > 0 else order.quantity
        payload = self._broker_order_payload(order)
        if trade is None:
            trade = TradeLog(
                market=intent.market,
                symbol=intent.symbol,
                side=intent.side,
                quantity=format(quantity or Decimal(intent.quantity or "0"), "f"),
                price=str(execution_price or intent.price or "0"),
                order_id=order.order_id,
                status=order.status,
                rationale="주문내역 자동 복구",
                raw={"order_detail": payload, "stock_name": intent.raw.get("stock_name")},
            )
            session.add(trade)
        else:
            previous_raw = dict(trade.raw or {})
            previous_raw["order_detail"] = payload
            trade.raw = previous_raw
            trade.status = order.status
            if quantity is not None and quantity > 0:
                trade.quantity = format(quantity, "f")
            if execution_price is not None:
                trade.price = format(execution_price, "f")
        return trade

    async def _reconcile_orders(self, prices: dict[str, Decimal]) -> None:
        if self.settings.broker_mode != "toss":
            return
        async with SessionLocal() as session:
            await self._backfill_order_intents(session)
            active_intents = (
                await session.scalars(
                    select(OrderIntent)
                    .where(OrderIntent.status.in_(ACTIVE_ORDER_STATUSES))
                    .order_by(OrderIntent.created_at)
                )
            ).all()
            now = datetime.now(timezone.utc)
            protected_source_ids = set(
                (
                    await session.scalars(select(ProtectionOrder.source_order_id))
                ).all()
            )
            recent_filled_buys = (
                await session.scalars(
                    select(OrderIntent).where(
                        OrderIntent.side == Action.BUY.value,
                        OrderIntent.status.in_({"FILLED", "CANCELED"}),
                        OrderIntent.created_at >= now - timedelta(days=2),
                    )
                )
            ).all()
            intents = list(active_intents)
            intents.extend(
                item
                for item in recent_filled_buys
                if (item.raw or {}).get("protect_with_oco")
                and item.order_id not in protected_source_ids
                and Decimal(
                    str((item.raw or {}).get("order_detail", {}).get("filled_quantity") or "0")
                )
                > 0
                and item.id not in {active.id for active in active_intents}
            )
            for intent in intents:
                created_at = self._aware_utc(intent.created_at) or now
                age_seconds = max(0, int((now - created_at).total_seconds()))
                try:
                    order = (
                        await self.broker.order_detail(intent.order_id)
                        if intent.order_id
                        else await self.broker.find_order(
                            intent.client_order_id, intent.symbol
                        )
                    )
                except (BrokerMaintenanceError, BrokerTransportError):
                    raise
                except BrokerError as exc:
                    intent.last_error = friendly_error_message(str(exc))
                    continue

                if order is None:
                    if (
                        intent.status in {"PREPARED", "AMBIGUOUS"}
                        and age_seconds >= self.settings.ambiguous_order_lookup_seconds
                    ):
                        intent.status = "NOT_FOUND"
                        intent.last_error = "토스 주문내역에서 확인되지 않아 재주문 가능 상태로 전환했습니다."
                        await audit(
                            session,
                            "ORDER_NOT_FOUND",
                            f"주문 확인 종료: {intent.market} {intent.symbol} - 토스 주문내역에 없음",
                            level="WARNING",
                            details={"client_order_id": intent.client_order_id},
                        )
                    continue

                previous_status = intent.status
                intent.order_id = order.order_id
                intent.status = order.status
                intent.last_error = None
                intent.raw = {**(intent.raw or {}), "order_detail": self._broker_order_payload(order)}
                trade = await self._upsert_trade_from_order(session, intent, order)
                decision = (
                    await session.get(DecisionLog, intent.decision_id)
                    if intent.decision_id
                    else None
                )
                if decision is not None:
                    decision.order_id = order.order_id
                    decision.status = order.status

                if previous_status != order.status:
                    await audit(
                        session,
                        "ORDER_STATUS_CHANGED",
                        f"주문 상태 변경: {intent.market} {intent.symbol} {previous_status} → {order.status}",
                        details={
                            "order_id": order.order_id,
                            "filled_quantity": format(order.filled_quantity, "f"),
                            "average_filled_price": (
                                format(order.average_filled_price, "f")
                                if order.average_filled_price is not None
                                else None
                            ),
                        },
                    )
                    try:
                        await self.notifier.order_status(intent, order)
                    except Exception:
                        logger.exception("Telegram 체결 상태 알림 전송 실패")

                if (
                    order.status in ACTIVE_ORDER_STATUSES
                    and age_seconds >= self.settings.pending_order_timeout_seconds
                    and intent.order_id
                ):
                    try:
                        await self.broker.cancel_order(intent.order_id)
                        intent.status = "PENDING_CANCEL"
                        trade.status = "PENDING_CANCEL"
                        await audit(
                            session,
                            "STALE_ORDER_CANCELED",
                            f"미체결 주문 자동 취소 요청: {intent.market} {intent.symbol} ({age_seconds}초 경과)",
                            level="WARNING",
                            details={"order_id": intent.order_id},
                        )
                    except BrokerError as exc:
                        intent.last_error = friendly_error_message(str(exc))

                if order.status in TERMINAL_ORDER_STATUSES and order.filled_quantity > 0:
                    await self._create_oco_for_fill(session, intent, order, prices)

            await session.commit()

    async def _create_oco_for_fill(
        self,
        session,
        intent: OrderIntent,
        order: BrokerOrder,
        prices: dict[str, Decimal],
    ) -> None:
        options = intent.raw or {}
        if intent.side != Action.BUY.value or not options.get("protect_with_oco"):
            return
        if await session.scalar(
            select(ProtectionOrder.id).where(
                ProtectionOrder.source_order_id == order.order_id
            )
        ):
            return
        entry = order.average_filled_price
        if entry is None or entry <= 0:
            return
        market = Market(intent.market)
        take_pct = Decimal(str(options.get("oco_take_profit_pct", 8.0))) / Decimal("100")
        stop_pct = Decimal(str(options.get("oco_stop_loss_pct", 4.0))) / Decimal("100")
        take_price = self._price_on_tick(entry * (Decimal("1") + take_pct), market)
        stop_trigger = self._price_on_tick(entry * (Decimal("1") - stop_pct), market)
        tick = self._tick_size(stop_trigger, market)
        stop_order = max(tick, stop_trigger - tick)
        current_price = prices.get(intent.symbol.upper()) or entry
        quantity = min(
            order.filled_quantity,
            await self.broker.sellable_quantity(intent.symbol),
        )
        if quantity <= 0:
            return
        client_order_id = f"aisa-oco-{uuid.uuid4().hex[:20]}"
        protection = ProtectionOrder(
            source_order_id=order.order_id,
            client_order_id=client_order_id,
            market=intent.market,
            symbol=intent.symbol,
            quantity=format(quantity, "f"),
            entry_price=format(entry, "f"),
            take_profit_price=format(take_price, "f"),
            stop_trigger_price=format(stop_trigger, "f"),
            stop_order_price=format(stop_order, "f"),
            status="PREPARING",
        )
        session.add(protection)
        await session.commit()

        if not (take_price > current_price > stop_trigger):
            protection.status = "SKIPPED"
            protection.last_error = "현재가가 OCO 감시가 범위를 벗어나 자동 등록하지 않았습니다."
            await audit(
                session,
                "OCO_SKIPPED",
                f"OCO 보호주문 보류: {intent.symbol} - 현재가가 익절·손절 범위 밖",
                level="WARNING",
                details={
                    "current_price": format(current_price, "f"),
                    "take_profit_price": format(take_price, "f"),
                    "stop_trigger_price": format(stop_trigger, "f"),
                },
            )
            await session.commit()
            return

        try:
            result = await self.broker.create_oco_order(
                symbol=intent.symbol,
                quantity=quantity,
                client_order_id=client_order_id,
                take_profit_price=take_price,
                stop_trigger_price=stop_trigger,
                stop_order_price=stop_order,
                expire_date=(datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat(),
            )
        except BrokerTransportError as exc:
            protection.status = "UNCERTAIN"
            protection.last_error = friendly_error_message(str(exc))
            await session.commit()
            return
        except BrokerError as exc:
            protection.status = "FAILED"
            protection.last_error = friendly_error_message(str(exc))
            await audit(
                session,
                "OCO_FAILURE",
                f"OCO 보호주문 등록 실패: {intent.symbol} - {protection.last_error}",
                level="WARNING",
            )
            await session.commit()
            return

        protection.conditional_order_id = result["conditionalOrderId"]
        protection.status = "WATCHING"
        protection.raw = result
        await audit(
            session,
            "OCO_CREATED",
            f"OCO 보호주문 등록: {intent.symbol} · 익절 {take_price} · 손절 {stop_trigger}",
            details={
                "conditional_order_id": protection.conditional_order_id,
                "source_order_id": order.order_id,
            },
        )
        await session.commit()
        try:
            await self.notifier.protection_created(protection)
        except Exception:
            logger.exception("Telegram OCO 알림 전송 실패")

    async def _reconcile_protections(self, snapshot=None) -> None:
        if self.settings.broker_mode != "toss":
            return
        async with SessionLocal() as session:
            rows = (
                await session.scalars(
                    select(ProtectionOrder).where(
                        ProtectionOrder.status.in_(ACTIVE_PROTECTION_STATUSES)
                    )
                )
            ).all()
            now = datetime.now(timezone.utc)
            holding_quantities = {
                item.symbol.upper(): item.quantity for item in (snapshot.holdings if snapshot else [])
            }
            for row in rows:
                created_at = self._aware_utc(row.created_at) or now
                protection_age = max(0, int((now - created_at).total_seconds()))
                held_quantity = holding_quantities.get(row.symbol.upper())
                if (
                    snapshot is not None
                    and protection_age >= 180
                    and held_quantity is not None
                    and held_quantity < Decimal(row.quantity)
                ) or (
                    snapshot is not None
                    and protection_age >= 180
                    and held_quantity is None
                ):
                    if row.conditional_order_id:
                        try:
                            await self.broker.cancel_conditional_order(row.conditional_order_id)
                        except BrokerError as exc:
                            row.last_error = friendly_error_message(str(exc))
                            continue
                    row.status = "CANCELED"
                    row.last_error = "실제 보유수량이 보호수량보다 적어 과매도 방지를 위해 취소했습니다."
                    await audit(
                        session,
                        "OCO_CANCELED",
                        f"보유수량 변경 감지로 OCO 자동 취소: {row.symbol}",
                        level="WARNING",
                        details={
                            "protected_quantity": row.quantity,
                            "holding_quantity": (
                                format(held_quantity, "f") if held_quantity is not None else "0"
                            ),
                        },
                    )
                    continue
                try:
                    detail = (
                        await self.broker.conditional_order_detail(row.conditional_order_id)
                        if row.conditional_order_id
                        else await self.broker.find_conditional_order(
                            row.client_order_id, row.symbol
                        )
                    )
                except (BrokerMaintenanceError, BrokerTransportError):
                    raise
                except BrokerError as exc:
                    row.last_error = friendly_error_message(str(exc))
                    continue
                if detail is None:
                    created_at = self._aware_utc(row.created_at) or now
                    if (now - created_at).total_seconds() >= self.settings.ambiguous_order_lookup_seconds:
                        row.status = "NOT_FOUND"
                        row.last_error = "토스 조건주문 내역에서 확인되지 않습니다."
                    continue
                previous_status = row.status
                row.conditional_order_id = detail.get("conditionalOrderId") or row.conditional_order_id
                row.status = detail.get("status") or row.status
                row.raw = detail
                row.last_error = None
                if row.status != previous_status:
                    await audit(
                        session,
                        "OCO_STATUS_CHANGED",
                        f"OCO 상태 변경: {row.symbol} {previous_status} → {row.status}",
                        details={"conditional_order_id": row.conditional_order_id},
                    )
            await session.commit()

    async def _cancel_symbol_protections(self, session, symbol: str) -> bool:
        if self.settings.broker_mode != "toss":
            return True
        rows = (
            await session.scalars(
                select(ProtectionOrder).where(
                    ProtectionOrder.symbol == symbol,
                    ProtectionOrder.status.in_(ACTIVE_PROTECTION_STATUSES),
                )
            )
        ).all()
        for row in rows:
            try:
                if row.conditional_order_id is None:
                    detail = await self.broker.find_conditional_order(
                        row.client_order_id, row.symbol
                    )
                    if detail is None:
                        row.last_error = "조건주문 접수 여부 확인 중이라 매도를 보류합니다."
                        return False
                    row.conditional_order_id = detail.get("conditionalOrderId")
                await self.broker.cancel_conditional_order(row.conditional_order_id)
                row.status = "CANCELED"
                await audit(
                    session,
                    "OCO_CANCELED",
                    f"신규 매도를 위해 기존 OCO 보호주문 취소: {symbol}",
                    details={"conditional_order_id": row.conditional_order_id},
                )
            except BrokerError as exc:
                row.last_error = friendly_error_message(str(exc))
                return False
        return True

    async def _run_cycle(self) -> None:
        sessions = {
            Market.KR: await self.broker.market_session(Market.KR),
            Market.US: await self.broker.market_session(Market.US),
        }
        async with SessionLocal() as session:
            state = await get_state(session)
            trading_profile = state.trading_profile or DEFAULT_PROFILE
            extended_hours_enabled = state.extended_hours_enabled
            day_market_enabled = state.day_market_enabled
            market_open = {
                market: self._trading_enabled_for_session(
                    market,
                    market_session,
                    extended_hours_enabled,
                    day_market_enabled,
                    trading_profile,
                )
                for market, market_session in sessions.items()
            }
            open_kr, open_us = market_open[Market.KR], market_open[Market.US]
            profile_limits = limits_for_profile(self.settings, trading_profile)
            state.last_cycle_at = datetime.now(timezone.utc)
            next_market_open = {market.value: value for market, value in market_open.items()}
            next_market_sessions = {
                market.value: self._session_payload(
                    market,
                    market_session,
                    extended_hours_enabled,
                    day_market_enabled,
                    trading_profile,
                )
                for market, market_session in sessions.items()
            }
            await self._audit_market_availability_changes(
                session, state.market_sessions, next_market_sessions
            )
            state.market_open = next_market_open
            state.market_sessions = next_market_sessions
            if not open_kr and not open_us:
                state.current_strategy = "거래 가능 장 개장 대기"
                state.consecutive_failures = 0
                await session.commit()
                return

        snapshot = await self.broker.account_snapshot()
        discovered = await self._discover_candidates(
            [holding.symbol for holding in snapshot.holdings],
            open_kr,
            open_us,
        )
        symbols = {
            holding.symbol.upper()
            for holding in snapshot.holdings
            if (holding.market == Market.KR and open_kr)
            or (holding.market == Market.US and open_us)
        }
        if open_kr:
            symbols.update(self.settings.kr_symbols)
        if open_us:
            symbols.update(self.settings.us_symbols)
        symbols.update(discovered)
        ordered_symbols = sorted(symbols)
        prices, stocks = await self.broker.prices(ordered_symbols), await self.broker.stock_info(
            ordered_symbols
        )
        await self._record_stock_reference_statuses(stocks)
        holding_weights = []
        for holding in snapshot.holdings:
            equity = snapshot.equity_krw if holding.market == Market.KR else snapshot.equity_usd
            weight_pct = Decimal("0")
            if equity > 0:
                weight_pct = (holding.market_value / equity) * Decimal("100")
            holding_weights.append(
                {
                    "symbol": holding.symbol,
                    "name": holding.name,
                    "market": holding.market.value,
                    "market_value": str(holding.market_value),
                    "weight_pct": str(weight_pct.quantize(Decimal("0.01"))),
                    "profit_rate_pct": str(
                        (holding.profit_rate * Decimal("100")).quantize(Decimal("0.01"))
                    ),
                }
            )
        concentration_threshold_pct = Decimal(str(profile_limits.max_position_weight * 100))
        concentrated_holdings = [
            item
            for item in holding_weights
            if Decimal(item["weight_pct"]) > concentration_threshold_pct
        ]
        cash_ratio_krw = (
            (snapshot.cash_krw / snapshot.equity_krw) * Decimal("100")
            if snapshot.equity_krw > 0
            else Decimal("0")
        )
        cash_ratio_usd = (
            (snapshot.cash_usd / snapshot.equity_usd) * Decimal("100")
            if snapshot.equity_usd > 0
            else Decimal("0")
        )

        market_data = {
            "regular_market_open": {
                "KR": sessions[Market.KR] == MarketSession.REGULAR,
                "US": sessions[Market.US] == MarketSession.REGULAR,
            },
            "market_sessions": {
                market.value: self._session_payload(
                    market,
                    market_session,
                    extended_hours_enabled,
                    day_market_enabled,
                    trading_profile,
                )
                for market, market_session in sessions.items()
            },
            "extended_hours_enabled": extended_hours_enabled,
            "day_market_enabled": day_market_enabled,
            "extended_hours_rule": (
                "정규장 외 프리·애프터마켓 주문은 지정가만 허용하며 "
                f"현재가 기준 ±{self.settings.extended_limit_price_buffer_pct * 100:.2f}% 이내로 제한한다."
            ),
            "day_market_rule": (
                "미국 데이마켓은 프리·애프터 거래 허용, 데이마켓 별도 토글, "
                "그리고 공격적 또는 최대수익 행동패턴이 모두 충족될 때만 거래한다."
            ),
            "candidate_universe": ordered_symbols,
            "prices": {key: str(value) for key, value in prices.items()},
            "stock_info": {
                key: value.model_dump(mode="json") for key, value in stocks.items()
            },
            "investment_profile": profile_ai_context(self.settings, trading_profile),
            "portfolio_rotation_context": {
                "cash_ratio_krw_pct": str(cash_ratio_krw.quantize(Decimal("0.01"))),
                "cash_ratio_usd_pct": str(cash_ratio_usd.quantize(Decimal("0.01"))),
                "holding_weights": holding_weights,
                "concentrated_holdings": concentrated_holdings,
                "rotation_instruction": (
                    "현금이 부족하고 특정 보유 종목 비중이 높으면, 더 강한 단기·스윙 기회가 확인된 후보로 "
                    "자금을 옮기기 위해 기존 보유 종목 일부 SELL 제안을 검토한다. 단, 매도는 보유수량 안에서만 "
                    "하고, 신규 BUY는 예수금과 위험 한도를 넘기지 않는다."
                ),
            },
            "hard_limits": {
                "profile": get_profile(trading_profile).label,
                "min_confidence_pct": profile_limits.min_confidence * 100,
                "max_position_weight_pct": profile_limits.max_position_weight * 100,
                "max_order_weight_pct": profile_limits.max_order_weight * 100,
                "min_cash_reserve_pct": profile_limits.min_cash_reserve * 100,
                "max_daily_loss_pct": profile_limits.max_daily_loss * 100,
                "max_daily_orders": profile_limits.max_daily_orders,
                "max_risk_score": profile_limits.max_risk_score,
                "cooldown_hours": profile_limits.cooldown_hours,
                "force_hold": profile_limits.force_hold,
            },
        }
        decision = await self.ai.analyze(snapshot, market_data)

        async with SessionLocal() as session:
            state = await get_state(session)
            state.current_strategy = decision.market_regime
            state.market_view = decision.market_summary
            state.active_broker_mode = self.settings.broker_mode
            state.latest_account = self._account_payload(snapshot)
            state.latest_prices = {key: str(value) for key, value in prices.items()}
            state.market_open = {market.value: value for market, value in market_open.items()}
            state.market_sessions = {
                market.value: self._session_payload(
                    market,
                    market_session,
                    extended_hours_enabled,
                    day_market_enabled,
                    trading_profile,
                )
                for market, market_session in sessions.items()
            }
            state.last_market_poll_at = datetime.now(timezone.utc)
            add_portfolio_snapshot(session, snapshot, self.settings.broker_mode)
            if state.circuit_breaker:
                await audit(
                    session,
                    "CIRCUIT_BREAKER",
                    "차단기가 활성화되어 분석만 기록하고 주문은 중단했습니다.",
                    level="WARNING",
                )

            for proposal in decision.proposals:
                await self._process_proposal(
                    session=session,
                    proposal=proposal,
                    snapshot=snapshot,
                    prices=prices,
                    stocks=stocks,
                    market_open=market_open,
                    market_sessions=sessions,
                    extended_hours_enabled=extended_hours_enabled,
                    market_summary=decision.market_summary,
                    trading_allowed=state.trading_armed and not state.circuit_breaker,
                    trading_profile=trading_profile,
                    profile_limits=profile_limits,
                )

            state.consecutive_failures = 0
            await audit(
                session,
                "ANALYSIS_COMPLETE",
                f"투자 분석 완료: {len(decision.proposals)}개 제안",
                details={"regime": decision.market_regime},
            )
            await session.commit()

    async def _discover_candidates(
        self,
        holdings: list[str],
        open_kr: bool,
        open_us: bool,
    ) -> list[str]:
        markets = []
        if open_kr:
            markets.append("KR")
        if open_us:
            markets.append("US")
        async with SessionLocal() as session:
            state = await get_state(session)
            cached = list(state.discovered_symbols or [])
            try:
                result = await self.ai.discover(holdings, markets)
                candidates = [
                    item.symbol.upper()
                    for item in result.candidates
                    if (item.market == Market.KR and open_kr)
                    or (item.market == Market.US and open_us)
                ]
                state.discovered_symbols = candidates
                await audit(
                    session,
                    "STOCK_DISCOVERY",
                    f"시장 전체 신규 후보 {len(candidates)}개 발굴",
                    details={"symbols": candidates},
                )
                await session.commit()
                return candidates
            except Exception as exc:
                message = friendly_error_message(str(exc))
                await audit(
                    session,
                    "DISCOVERY_FAILURE",
                    message,
                    level="WARNING",
                    details={"using_cached_symbols": cached},
                )
                await session.commit()
                return cached

    async def _process_proposal(
        self,
        *,
        session,
        proposal: TradeProposal,
        snapshot,
        prices,
        stocks,
        market_open,
        market_sessions,
        extended_hours_enabled: bool,
        market_summary: str,
        trading_allowed: bool,
        trading_profile: str,
        profile_limits,
    ) -> None:
        symbol = proposal.symbol.upper()
        evidence = [item.model_dump(mode="json") for item in proposal.evidence]
        if proposal.action == Action.HOLD and market_open.get(proposal.market, False):
            return
        log = DecisionLog(
            market=proposal.market.value,
            symbol=symbol,
            action=proposal.action.value,
            confidence=proposal.confidence,
            thesis=proposal.thesis,
            evidence=evidence,
            expected_return_pct=proposal.expected_return_pct,
            risk_score=proposal.risk_score,
            status="VALIDATING",
        )
        session.add(log)
        await session.flush()

        if proposal.action == Action.HOLD:
            log.status = "HOLD"
            log.rejection_reasons = ["AI가 관망을 선택했습니다."]
            return
        if symbol not in prices or symbol not in stocks:
            log.status = "REJECTED"
            log.rejection_reasons = ["공식 시세 또는 종목 정보를 확인할 수 없습니다."]
            return

        active_intent = await session.scalar(
            select(OrderIntent.id)
            .where(
                OrderIntent.symbol == symbol,
                OrderIntent.status.in_(ACTIVE_ORDER_STATUSES),
            )
            .limit(1)
        )
        if active_intent:
            log.status = "ORDER_PENDING"
            log.rejection_reasons = ["동일 종목의 기존 주문이 처리 중이라 새 주문을 보내지 않습니다."]
            return

        quote_timestamp = self._aware_utc(self.broker.price_timestamp(symbol))
        if self.settings.broker_mode == "toss":
            if quote_timestamp is None:
                log.status = "STALE_QUOTE"
                log.rejection_reasons = ["시세 기준시각을 확인할 수 없어 주문을 보류했습니다."]
                return
            quote_age = max(
                0,
                int((datetime.now(timezone.utc) - quote_timestamp).total_seconds()),
            )
            if quote_age > self.settings.max_quote_age_seconds:
                log.status = "STALE_QUOTE"
                log.rejection_reasons = [
                    f"시세가 {quote_age}초 전에 갱신되어 주문 기준({self.settings.max_quote_age_seconds}초)을 넘었습니다."
                ]
                return

        cooldown_since = datetime.now(timezone.utc) - timedelta(hours=profile_limits.cooldown_hours)
        recent_trade = await session.scalar(
            select(TradeLog.id)
            .where(TradeLog.symbol == symbol, TradeLog.created_at >= cooldown_since)
            .limit(1)
        )
        if recent_trade:
            log.status = "COOLDOWN"
            log.rejection_reasons = [
                f"동일 종목은 최근 거래 후 {profile_limits.cooldown_hours}시간이 지나야 다시 거래합니다."
            ]
            return

        holding = next((item for item in snapshot.holdings if item.symbol.upper() == symbol), None)
        current_quantity = holding.quantity if holding else Decimal("0")
        current_value = holding.market_value if holding else Decimal("0")
        equity = snapshot.equity_krw if proposal.market == Market.KR else snapshot.equity_usd
        currency = "KRW" if proposal.market == Market.KR else "USD"
        price = prices[symbol]
        current_session = market_sessions[proposal.market]
        is_extended_session = self._is_extended_session(current_session)
        order_type = "LIMIT" if is_extended_session else "MARKET"
        use_us_amount_buy = (
            proposal.market == Market.US
            and proposal.action == Action.BUY
            and current_session == MarketSession.REGULAR
        )
        order_price = (
            self._limit_price_for_extended_session(price, proposal.market, proposal.action)
            if is_extended_session
            else None
        )
        order_value_price = order_price or price
        desired_value = equity * Decimal(str(proposal.target_weight_pct / 100))

        if proposal.action == Action.BUY:
            delta_value = max(Decimal("0"), desired_value - current_value)
            max_order = equity * Decimal(str(profile_limits.max_order_weight))
            proposed_notional = min(delta_value, max_order)
            if use_us_amount_buy:
                proposed_notional = proposed_notional.quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                )
                quantity = (proposed_notional / order_value_price).quantize(
                    Decimal("0.000001"), rounding=ROUND_DOWN
                )
            else:
                quantity = (proposed_notional / order_value_price).to_integral_value(
                    rounding=ROUND_DOWN
                )
        else:
            delta_value = max(Decimal("0"), current_value - desired_value)
            raw_quantity = min(current_quantity, delta_value / price)
            quantity = (
                raw_quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                if proposal.market == Market.US and not is_extended_session
                else raw_quantity.to_integral_value(rounding=ROUND_DOWN)
            )
            proposed_notional = quantity * order_value_price
            min_remaining_position = self._minimum_remaining_position_amount(proposal.market)
            remaining_position_value = max(Decimal("0"), current_value - proposed_notional)
            if Decimal("0") < remaining_position_value < min_remaining_position:
                adjusted_quantity = current_quantity
                if proposal.market == Market.US and is_extended_session:
                    adjusted_quantity = adjusted_quantity.to_integral_value(rounding=ROUND_DOWN)
                    if adjusted_quantity <= 0:
                        log.status = "REJECTED"
                        log.rejection_reasons = [
                            "프리·애프터·데이마켓에서는 미국 주식 소수점 잔량을 지정가로 매도할 수 없어 주문을 보류했습니다."
                        ]
                        return
                elif proposal.market == Market.KR:
                    adjusted_quantity = adjusted_quantity.to_integral_value(rounding=ROUND_DOWN)
                quantity = adjusted_quantity
                proposed_notional = quantity * order_value_price

        start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        daily_orders = await session.scalar(
            select(func.count(TradeLog.id)).where(TradeLog.created_at >= start_of_day)
        )
        buying_power = await self.broker.buying_power(currency)
        sellable = (
            await self.broker.sellable_quantity(symbol)
            if proposal.action == Action.SELL
            else Decimal("0")
        )
        min_order_amount = self._minimum_order_amount(proposal.market)
        if proposal.action == Action.BUY:
            if buying_power < min_order_amount:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"주문 금액 부족: {currency} 매수 가능 금액 {buying_power}이 "
                    f"최소 주문 기준 {min_order_amount} {currency}보다 작습니다."
                ]
                return
            if proposed_notional < min_order_amount:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"주문 금액 부족: 예상 주문금액 {proposed_notional} {currency}가 "
                    f"최소 주문 기준 {min_order_amount} {currency}보다 작습니다. "
                    f"현재 매수 가능 금액은 {buying_power} {currency}입니다."
                ]
                return
            if not use_us_amount_buy and quantity <= 0:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"주문 수량 부족: 현재 {current_session.value} 세션은 정수 수량 주문만 가능하며, "
                    f"{symbol} 1주 가격 {order_value_price} {currency}보다 주문 예산 "
                    f"{proposed_notional} {currency}가 작습니다."
                ]
                return
            remaining_cash = buying_power - proposed_notional
            if Decimal("0") < remaining_cash < min_order_amount:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"주문 금액 조건 불충족: 주문 후 남는 현금 {remaining_cash} {currency}가 "
                    f"최소 주문 기준 {min_order_amount} {currency}보다 작습니다."
                ]
                return
        elif proposal.action == Action.SELL:
            full_sell = quantity >= current_quantity
            if proposed_notional < min_order_amount and not full_sell:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"예상 매도금액이 최소 주문 기준({min_order_amount} {currency})보다 작아 주문을 보류했습니다."
                ]
                return
            remaining_position_value = max(Decimal("0"), current_value - proposed_notional)
            min_remaining_position = self._minimum_remaining_position_amount(proposal.market)
            if Decimal("0") < remaining_position_value < min_remaining_position:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"매도 후 남는 보유 잔량 평가금액이 최소 잔량 기준({min_remaining_position} {currency})보다 작아 주문을 보류했습니다."
                ]
                return
        warning_codes = await self.broker.warnings(symbol)
        warning_labels = [WARNING_LABELS.get(code, code) for code in warning_codes]
        await self._audit_special_status(
            session,
            symbol=symbol,
            name=stocks[symbol].name,
            labels=warning_labels,
            codes=warning_codes,
        )
        risk_context = RiskContext(
            market_open=market_open[proposal.market],
            market_session=current_session,
            extended_hours_enabled=extended_hours_enabled,
            order_type=order_type,
            stock=stocks[symbol],
            warnings=warning_codes,
            buying_power=buying_power,
            sellable_quantity=sellable,
            current_quantity=current_quantity,
            current_position_value=current_value,
            portfolio_equity=equity,
            daily_return=snapshot.daily_return,
            daily_order_count=int(daily_orders or 0),
            proposed_quantity=quantity,
            proposed_notional=proposed_notional,
        )
        result = self.risk.evaluate(
            proposal,
            risk_context,
            profile_key=trading_profile,
            limits=profile_limits,
        )
        if not result.approved:
            log.status = "REJECTED"
            log.rejection_reasons = result.reasons
            return
        if not trading_allowed:
            log.status = "NOT_ARMED"
            log.rejection_reasons = ["자동매매가 중지 상태이거나 차단기가 활성화되어 있습니다."]
            return
        if self.settings.broker_mode == "toss" and not self.settings.live_trading_enabled:
            log.status = "LIVE_DISABLED"
            log.rejection_reasons = ["서버의 LIVE_TRADING_ENABLED가 false입니다."]
            return

        if proposal.action == Action.SELL:
            protections_canceled = await self._cancel_symbol_protections(session, symbol)
            if not protections_canceled:
                log.status = "PROTECTION_CANCEL_PENDING"
                log.rejection_reasons = [
                    "기존 OCO 보호주문의 취소 여부를 확인 중이라 중복 매도를 막기 위해 주문을 보류했습니다."
                ]
                return
            sellable = await self.broker.sellable_quantity(symbol)
            if sellable < quantity:
                log.status = "REJECTED"
                log.rejection_reasons = [
                    f"OCO 취소 후 매도 가능 수량 {sellable}주가 주문 수량 {quantity}주보다 작습니다."
                ]
                return

        order_amount = proposed_notional if use_us_amount_buy else None
        order_quantity = None if use_us_amount_buy else quantity
        client_order_id = f"aisa-{uuid.uuid4().hex[:24]}"
        order = OrderRequest(
            symbol=symbol,
            market=proposal.market,
            action=proposal.action,
            quantity=order_quantity,
            order_amount=order_amount,
            order_type=order_type,
            price=order_price,
            market_session=current_session,
            client_order_id=client_order_id,
        )
        state = await get_state(session)
        intent = OrderIntent(
            client_order_id=client_order_id,
            decision_id=log.id,
            market=proposal.market.value,
            symbol=symbol,
            side=proposal.action.value,
            quantity=format(order_quantity, "f") if order_quantity is not None else None,
            order_amount=format(order_amount, "f") if order_amount is not None else None,
            order_type=order_type,
            price=format(order_price, "f") if order_price is not None else None,
            status="PREPARED",
            raw={
                "stock_name": stocks[symbol].name,
                "market_session": current_session.value,
                "protect_with_oco": bool(
                    self.settings.broker_mode == "toss"
                    and proposal.action == Action.BUY
                    and state.oco_enabled
                ),
                "oco_take_profit_pct": state.oco_take_profit_pct,
                "oco_stop_loss_pct": state.oco_stop_loss_pct,
            },
        )
        session.add(intent)
        log.status = "ORDER_PREPARED"
        await session.commit()
        try:
            order_result = await self.broker.place_order(order)
        except BrokerTransportError as exc:
            message = friendly_error_message(str(exc))
            intent.status = "AMBIGUOUS"
            intent.last_error = message
            log.status = "ORDER_STATUS_UNKNOWN"
            log.rejection_reasons = [
                "주문 응답이 끊겨 접수 여부를 확인 중입니다. 중복 주문은 보내지 않습니다."
            ]
            await audit(
                session,
                "ORDER_STATUS_UNKNOWN",
                f"주문 응답 확인 필요: {proposal.market.value} {symbol} - 주문내역 자동 조회 중",
                level="WARNING",
                details={"client_order_id": client_order_id, "message": message},
            )
            await session.commit()
            return
        except BrokerMaintenanceError:
            intent.status = "AMBIGUOUS"
            log.status = "ORDER_STATUS_UNKNOWN"
            await session.commit()
            raise
        except BrokerError as exc:
            message = friendly_error_message(str(exc))
            intent.status = "REJECTED"
            intent.last_error = message
            log.status = "REJECTED_ORDER"
            log.rejection_reasons = [message]
            await audit(
                session,
                "ORDER_REJECTED",
                f"주문 거절: {proposal.market.value} {symbol} - {message}",
                level="WARNING",
                details={
                    "market": proposal.market.value,
                    "symbol": symbol,
                    "action": proposal.action.value,
                    "quantity": format(quantity, "f"),
                    "order_amount": format(order_amount, "f") if order_amount else None,
                    "order_type": order_type,
                    "market_session": current_session.value,
                    "message": message,
                },
            )
            await session.commit()
            return
        intent.order_id = order_result.order_id
        intent.status = order_result.status
        intent.raw = {**(intent.raw or {}), "submit_response": order_result.raw}
        log.status = order_result.status
        log.order_id = order_result.order_id
        session.add(
            TradeLog(
                market=proposal.market.value,
                symbol=symbol,
                side=proposal.action.value,
                quantity=format(quantity, "f"),
                price=str(order_price or price),
                order_id=order_result.order_id,
                status=order_result.status,
                rationale=proposal.thesis,
                raw={
                    **order_result.raw,
                    "stock_name": stocks[symbol].name,
                    "client_order_id": client_order_id,
                    "reference_price": str(price),
                    "order_type": order_type,
                    "order_amount": format(order_amount, "f") if order_amount else None,
                    "market_session": current_session.value,
                    "limit_buffer_pct": self.settings.extended_limit_price_buffer_pct,
                },
            )
        )
        await session.commit()
        refreshed_snapshot = await self.broker.account_snapshot()
        state = await get_state(session)
        state.active_broker_mode = self.settings.broker_mode
        state.latest_account = self._account_payload(refreshed_snapshot)
        try:
            await self.notifier.trade(proposal, order, order_result, market_summary)
        except Exception as exc:
            logger.exception("Telegram 주문 알림 전송 실패: %s %s", proposal.market.value, symbol)
            await audit(
                session,
                "TELEGRAM_TRADE_FAILURE",
                f"Telegram 주문 알림 전송 실패: {proposal.market.value} {symbol} - {exc}",
                level="WARNING",
                details={
                    "market": proposal.market.value,
                    "symbol": symbol,
                    "order_id": order_result.order_id,
                },
            )

    async def _record_failure(self, reason: str) -> None:
        stopped = False
        async with SessionLocal() as session:
            state = await get_state(session)
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.settings.max_consecutive_failures:
                state.circuit_breaker = True
                state.trading_armed = False
                state.breaker_reason = reason
                stopped = True
            await audit(
                session,
                "CYCLE_FAILURE",
                reason,
                level="ERROR",
                details={"consecutive_failures": state.consecutive_failures, "stopped": stopped},
            )
            await session.commit()
        try:
            await self.notifier.failure(reason, stopped)
        except Exception:
            logger.exception("Telegram 장애 알림 전송 실패")
