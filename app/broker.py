import asyncio
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import AuditLog, PaperCash, PaperHolding, SessionLocal, TradeLog
from app.schemas import (
    AccountSnapshot,
    Action,
    BrokerOrder,
    Holding,
    Market,
    OrderRequest,
    OrderResult,
    StockInfo,
    MarketSession,
)

KST = timezone(timedelta(hours=9), "KST")

KR_STOCK_NAMES = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "005380": "현대차",
    "068270": "셀트리온",
    "105560": "KB금융",
}
US_STOCK_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "META": "Meta Platforms",
    "BRK.B": "Berkshire Hathaway",
}


class BrokerError(RuntimeError):
    pass


class BrokerMaintenanceError(BrokerError):
    pass


class BrokerTransportError(BrokerError):
    """요청 결과를 알 수 없는 네트워크 오류. 재주문 전 반드시 주문내역을 확인한다."""

    pass


def _response_error_code(response: httpx.Response) -> str | None:
    """토스 오류 응답의 문자열/객체 형태를 모두 안전하게 읽는다."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("code")
    if isinstance(error, str):
        return error
    code = payload.get("code")
    return code if isinstance(code, str) else None


def friendly_error_message(message: str) -> str:
    text = str(message or "")
    if "trade.minimum-order-amount-for-remain" in text:
        return (
            "토스가 주문을 거절했습니다. 주문 후 남는 현금 또는 보유 잔량 금액이 너무 작습니다. "
            "최소 주문금액 이상으로 주문하거나, 애매한 소액 잔량이 남지 않도록 전량 매도해야 합니다."
        )
    if "order-limit-exceeded" in text:
        return "토스 주문 한도에 걸렸습니다. 주문 금액, 수량, 잔여 금액 또는 계좌 제한 조건을 확인해야 합니다."
    if "fractional-quantity-outside-regular-hours" in text:
        return "미국 주식 소수점 수량 매도는 정규장에서만 가능합니다. 프리·애프터·데이마켓에서는 정수 수량 지정가 주문만 허용됩니다."
    if "amount-order-outside-regular-hours" in text:
        return "미국 주식 금액 기반 소수점 매수는 정규장에서만 가능합니다. 프리·애프터·데이마켓에서는 정수 수량 지정가 주문만 허용됩니다."
    if "fractional-quantity-scale-exceeded" in text:
        return "미국 주식 소수점 수량은 소수점 6자리까지만 주문할 수 있습니다."
    if "invalid-request" in text and "fractional" in text:
        return "토스가 소수점 수량 주문을 거절했습니다. 매수는 금액 주문이 필요하고, 지정가/국내 주문은 소수점 수량을 사용할 수 없습니다."
    if "orderAmount" in text and "미국" in text:
        return "금액 주문은 미국 정규장 시장가 매수에서만 사용할 수 있습니다."
    if "IP address not allowed" in text or "access_denied" in text:
        return "토스 인증 실패: 현재 서버 IP가 토스 Open API 허용 IP에 등록되어 있지 않습니다."
    if "sidecar" in text.lower():
        return "사이드카 발동으로 시장 매수·매도 주문이 일시 제한되었습니다. 거래 재개 후 다시 시도합니다."
    if any(
        code in text.lower()
        for code in ("trading-suspended", "market-suspended", "market-halted")
    ):
        return "거래소 또는 해당 종목의 거래가 일시 중단되어 주문할 수 없습니다."
    if "insufficient_quota" in text or "You exceeded your current quota" in text:
        return "OpenAI API 사용 한도 또는 결제 한도가 부족합니다. OpenAI 결제/크레딧 상태를 확인해야 합니다."
    if "invalid_json_schema" in text:
        return "OpenAI 응답 형식 설정 오류입니다. 코드의 AI 응답 스키마를 수정해야 합니다."
    if "429" in text:
        return "외부 API 호출 한도에 걸렸습니다. 잠시 후 다시 시도하거나 사용량/결제 한도를 확인해야 합니다."
    return text


class Broker(ABC):
    @abstractmethod
    async def account_snapshot(self) -> AccountSnapshot: ...

    @abstractmethod
    async def prices(self, symbols: list[str]) -> dict[str, Decimal]: ...

    @abstractmethod
    async def stock_info(self, symbols: list[str]) -> dict[str, StockInfo]: ...

    @abstractmethod
    async def warnings(self, symbol: str) -> list[str]: ...

    @abstractmethod
    async def market_session(self, market: Market) -> MarketSession: ...

    async def market_open(self, market: Market) -> bool:
        return await self.market_session(market) != MarketSession.CLOSED

    @abstractmethod
    async def buying_power(self, currency: str) -> Decimal: ...

    @abstractmethod
    async def sellable_quantity(self, symbol: str) -> Decimal: ...

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def order_detail(self, order_id: str) -> BrokerOrder: ...

    @abstractmethod
    async def find_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def create_oco_order(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        take_profit_price: Decimal,
        stop_trigger_price: Decimal,
        stop_order_price: Decimal,
        expire_date: str,
    ) -> dict: ...

    @abstractmethod
    async def conditional_order_detail(self, conditional_order_id: str) -> dict: ...

    @abstractmethod
    async def find_conditional_order(self, client_order_id: str, symbol: str) -> dict | None: ...

    @abstractmethod
    async def cancel_conditional_order(self, conditional_order_id: str) -> None: ...

    def price_timestamp(self, symbol: str) -> datetime | None:
        return None

    async def close(self) -> None:
        return None


class TossBroker(Broker):
    """토스증권 Open API v1.2.4 REST 클라이언트."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.toss_base_url, timeout=15)
        self._token: str | None = None
        self._token_expires_at = datetime.now(timezone.utc)
        self._account_seq = settings.toss_account_seq
        self._last_price_timestamps: dict[str, datetime] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _access_token(self) -> str:
        if self._token and datetime.now(timezone.utc) < self._token_expires_at:
            return self._token
        secret = self.settings.toss_client_secret
        if not self.settings.toss_client_id or secret is None:
            raise BrokerError("토스증권 API 자격 증명이 없습니다.")
        try:
            response = await self.client.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.toss_client_id,
                    "client_secret": secret.get_secret_value(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise BrokerTransportError(f"토스 인증 서버 네트워크 오류: {exc}") from exc
        if response.is_error:
            error_code = _response_error_code(response)
            if error_code == "maintenance":
                raise BrokerMaintenanceError(
                    "토스증권 시스템 점검 중입니다. 점검 종료 후 자동으로 다시 연결합니다."
                )
            raise BrokerError(f"토스 인증 실패({response.status_code}): {response.text[:300]}")
        data = response.json()
        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(60, expires_in - 120)
        )
        return self._token

    async def _resolve_account(self) -> int:
        if self._account_seq is not None:
            return self._account_seq
        data = await self._request("GET", "/api/v1/accounts", account_required=False)
        accounts = data.get("result") or []
        account = next((x for x in accounts if x.get("accountType") == "BROKERAGE"), None)
        if not account:
            raise BrokerError("사용 가능한 종합매매(BROKERAGE) 계좌가 없습니다.")
        self._account_seq = int(account["accountSeq"])
        return self._account_seq

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        account_required: bool = False,
    ) -> dict:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_required:
            headers["X-Tossinvest-Account"] = str(await self._resolve_account())

        # 변경 요청은 네트워크 오류 시 재전송하지 않는다. 단, 서버가 주문을 처리하기 전
        # 반환하는 401에 한해서만 토큰을 갱신하고 한 번 다시 보낸다.
        attempts = 3 if method == "GET" else 2
        for attempt in range(attempts):
            try:
                response = await self.client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.RequestError as exc:
                if method != "GET" or attempt + 1 == attempts:
                    raise BrokerTransportError(f"토스증권 네트워크 오류: {exc}") from exc
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 401 and attempt == 0:
                self._token = None
                headers["Authorization"] = f"Bearer {await self._access_token()}"
                continue
            if response.is_error:
                error_code = _response_error_code(response)
                if error_code == "maintenance":
                    raise BrokerMaintenanceError(
                        "토스증권 시스템 점검 중입니다. 점검 종료 후 자동으로 다시 연결합니다."
                    )
                if method == "GET" and attempt + 1 < attempts and response.status_code in {429, 500, 502, 503, 504}:
                    delay = response.headers.get("Retry-After")
                    try:
                        wait_seconds = min(10.0, max(1.0, float(delay or 2**attempt)))
                    except ValueError:
                        wait_seconds = float(2**attempt)
                    await asyncio.sleep(wait_seconds)
                    continue
                friendly = friendly_error_message(response.text[:500])
                raise BrokerError(
                    f"{friendly} [토스 {method} {path} {response.status_code}]"
                )
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise BrokerError("토스 API 요청을 완료하지 못했습니다.")

    @staticmethod
    def _stock_name_from_item(item: dict) -> str | None:
        for key in (
            "name",
            "stockName",
            "koreanName",
            "korName",
            "securityName",
            "shortName",
            "displayName",
            "englishName",
        ):
            value = item.get(key)
            if value:
                return str(value)
        for nested_key in ("stock", "security", "instrument"):
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                for key in (
                    "name",
                    "stockName",
                    "koreanName",
                    "korName",
                    "securityName",
                    "shortName",
                    "displayName",
                    "englishName",
                ):
                    value = nested.get(key)
                    if value:
                        return str(value)
        return None

    async def account_snapshot(self) -> AccountSnapshot:
        data = await self._request("GET", "/api/v1/holdings", account_required=True)
        overview = data.get("result") or {}
        items = overview.get("items") or []
        symbols = [str(item.get("symbol", "")).upper() for item in items if item.get("symbol")]
        try:
            stock_infos = await self.stock_info(symbols)
        except BrokerError:
            stock_infos = {}
        holdings: list[Holding] = []
        for item in items:
            symbol = str(item["symbol"]).upper()
            market = Market.KR if item.get("marketCountry") == "KR" else Market.US
            profit = item.get("profitLoss") or {}
            market_value = item.get("marketValue") or {}
            stock_info = stock_infos.get(symbol)
            name = (
                self._stock_name_from_item(item)
                or (stock_info.name if stock_info else None)
                or KR_STOCK_NAMES.get(symbol)
                or US_STOCK_NAMES.get(symbol)
                or symbol
            )
            holdings.append(
                Holding(
                    symbol=symbol,
                    name=name,
                    market=market,
                    currency=item["currency"],
                    quantity=Decimal(item["quantity"]),
                    last_price=Decimal(item["lastPrice"]),
                    average_price=Decimal(item["averagePurchasePrice"]),
                    market_value=Decimal(market_value.get("amount", "0")),
                    profit_loss=Decimal(profit.get("amount", "0")),
                    profit_rate=Decimal(profit.get("rate", "0")),
                )
            )
        cash_krw, cash_usd = await asyncio.gather(
            self.buying_power("KRW"), self.buying_power("USD")
        )
        kr_value = sum((h.market_value for h in holdings if h.currency == "KRW"), Decimal("0"))
        us_value = sum((h.market_value for h in holdings if h.currency == "USD"), Decimal("0"))
        total_rate = Decimal((overview.get("profitLoss") or {}).get("rate", "0"))
        daily_rate = Decimal((overview.get("dailyProfitLoss") or {}).get("rate", "0"))
        return AccountSnapshot(
            captured_at=datetime.now(timezone.utc),
            holdings=holdings,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            equity_krw=cash_krw + kr_value,
            equity_usd=cash_usd + us_value,
            total_profit_rate=total_rate,
            daily_return=daily_rate,
        )

    async def prices(self, symbols: list[str]) -> dict[str, Decimal]:
        if not symbols:
            return {}
        result: dict[str, Decimal] = {}
        for start in range(0, len(symbols), 200):
            batch = symbols[start : start + 200]
            data = await self._request(
                "GET", "/api/v1/prices", params={"symbols": ",".join(batch)}
            )
            for item in data.get("result") or []:
                symbol = item["symbol"].upper()
                result[symbol] = Decimal(item["lastPrice"])
                timestamp = item.get("timestamp")
                if timestamp:
                    try:
                        self._last_price_timestamps[symbol] = datetime.fromisoformat(timestamp)
                    except (TypeError, ValueError):
                        pass
        return result

    def price_timestamp(self, symbol: str) -> datetime | None:
        return self._last_price_timestamps.get(symbol.upper())

    async def stock_info(self, symbols: list[str]) -> dict[str, StockInfo]:
        if not symbols:
            return {}
        data = await self._request(
            "GET", "/api/v1/stocks", params={"symbols": ",".join(symbols[:200])}
        )
        result: dict[str, StockInfo] = {}
        for item in data.get("result") or []:
            leverage = item.get("leverageFactor")
            korean_detail = item.get("koreanMarketDetail") or {}
            result[item["symbol"].upper()] = StockInfo(
                symbol=item["symbol"].upper(),
                name=item.get("name") or item.get("englishName") or item["symbol"],
                market_name=item.get("market", "UNKNOWN"),
                security_type=item.get("securityType", "UNKNOWN"),
                status=item.get("status", "UNKNOWN"),
                currency=item.get("currency", "KRW"),
                leverage_factor=Decimal(leverage) if leverage is not None else None,
                liquidation_trading=bool(korean_detail.get("liquidationTrading", False)),
                nxt_supported=bool(korean_detail.get("nxtSupported", False)),
                krx_trading_suspended=bool(
                    korean_detail.get("krxTradingSuspended", False)
                ),
                nxt_trading_suspended=korean_detail.get("nxtTradingSuspended"),
            )
        return result

    async def warnings(self, symbol: str) -> list[str]:
        data = await self._request("GET", f"/api/v1/stocks/{symbol}/warnings")
        return [item["warningType"] for item in data.get("result") or []]

    @staticmethod
    def _session_contains_now(session: dict | None, now: datetime) -> bool:
        if not session:
            return False
        start = datetime.fromisoformat(session["startTime"]).astimezone(timezone.utc)
        end = datetime.fromisoformat(session["endTime"]).astimezone(timezone.utc)
        return start <= now < end

    async def market_session(self, market: Market) -> MarketSession:
        path = f"/api/v1/market-calendar/{market.value}"
        data = await self._request("GET", path)
        result = data.get("result") or {}
        now = datetime.now(timezone.utc)
        if market == Market.KR:
            session_order = [
                ("preMarket", MarketSession.PRE),
                ("regularMarket", MarketSession.REGULAR),
                ("afterMarket", MarketSession.AFTER),
            ]
            for day_key in ("previousBusinessDay", "today", "nextBusinessDay"):
                sessions = ((result.get(day_key) or {}).get("integrated") or {})
                for api_key, session_type in session_order:
                    if self._session_contains_now(sessions.get(api_key), now):
                        return session_type
        else:
            session_order = [
                ("dayMarket", MarketSession.DAY),
                ("preMarket", MarketSession.PRE),
                ("regularMarket", MarketSession.REGULAR),
                ("afterMarket", MarketSession.AFTER),
            ]
            for day_key in ("previousBusinessDay", "today", "nextBusinessDay"):
                sessions = result.get(day_key) or {}
                for api_key, session_type in session_order:
                    if self._session_contains_now(sessions.get(api_key), now):
                        return session_type
        return MarketSession.CLOSED

    async def buying_power(self, currency: str) -> Decimal:
        data = await self._request(
            "GET",
            "/api/v1/buying-power",
            params={"currency": currency},
            account_required=True,
        )
        return Decimal((data.get("result") or {}).get("cashBuyingPower", "0"))

    async def sellable_quantity(self, symbol: str) -> Decimal:
        data = await self._request(
            "GET",
            "/api/v1/sellable-quantity",
            params={"symbol": symbol},
            account_required=True,
        )
        return Decimal((data.get("result") or {}).get("sellableQuantity", "0"))

    async def place_order(self, order: OrderRequest) -> OrderResult:
        payload: dict[str, Any] = {
            "clientOrderId": order.client_order_id,
            "symbol": order.symbol,
            "side": order.action.value,
            "orderType": order.order_type,
            "confirmHighValueOrder": False,
        }
        if order.order_amount is not None:
            if order.quantity is not None:
                raise BrokerError("주문 수량과 주문 금액을 동시에 지정할 수 없습니다.")
            if not (
                order.market == Market.US
                and order.action == Action.BUY
                and order.order_type == "MARKET"
                and order.market_session == MarketSession.REGULAR
            ):
                raise BrokerError("미국 정규장 시장가 매수에서만 금액 주문을 사용할 수 있습니다.")
            payload["orderAmount"] = format(order.order_amount, "f")
        elif order.quantity is not None:
            payload["timeInForce"] = order.time_in_force
            payload["quantity"] = format(order.quantity, "f")
            if order.order_type == "LIMIT" and order.price is not None:
                payload["price"] = format(order.price, "f")
        else:
            raise BrokerError("주문 수량 또는 주문 금액이 필요합니다.")
        data = await self._request(
            "POST", "/api/v1/orders", json=payload, account_required=True
        )
        result = data.get("result") or {}
        if not result.get("orderId"):
            raise BrokerError("주문 응답에 orderId가 없습니다.")
        return OrderResult(
            order_id=result["orderId"],
            client_order_id=result.get("clientOrderId") or order.client_order_id,
            status="SUBMITTED",
            raw=data,
        )

    @staticmethod
    def _parse_order(item: dict) -> BrokerOrder:
        execution = item.get("execution") or {}

        def decimal_or_none(value) -> Decimal | None:
            if value in (None, ""):
                return None
            return Decimal(str(value))

        ordered_at = item.get("orderedAt")
        return BrokerOrder(
            order_id=str(item.get("orderId") or ""),
            client_order_id=item.get("clientOrderId"),
            symbol=str(item.get("symbol") or "").upper(),
            side=str(item.get("side") or ""),
            status=str(item.get("status") or "UNKNOWN"),
            order_type=item.get("orderType"),
            quantity=decimal_or_none(item.get("quantity")),
            order_amount=decimal_or_none(item.get("orderAmount")),
            price=decimal_or_none(item.get("price")),
            ordered_at=datetime.fromisoformat(ordered_at) if ordered_at else None,
            filled_quantity=decimal_or_none(execution.get("filledQuantity")) or Decimal("0"),
            average_filled_price=decimal_or_none(execution.get("averageFilledPrice")),
            filled_amount=decimal_or_none(execution.get("filledAmount")),
            commission=decimal_or_none(execution.get("commission")),
            tax=decimal_or_none(execution.get("tax")),
            raw=item,
        )

    async def order_detail(self, order_id: str) -> BrokerOrder:
        data = await self._request(
            "GET", f"/api/v1/orders/{order_id}", account_required=True
        )
        return self._parse_order(data.get("result") or {})

    async def find_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        for lifecycle in ("OPEN", "CLOSED"):
            data = await self._request(
                "GET",
                "/api/v1/orders",
                params={"status": lifecycle, "symbol": symbol, "limit": 100},
                account_required=True,
            )
            result = data.get("result") or {}
            rows = result if isinstance(result, list) else result.get("orders") or result.get("items") or []
            for item in rows:
                if item.get("clientOrderId") == client_order_id:
                    return self._parse_order(item)
        return None

    async def cancel_order(self, order_id: str) -> None:
        await self._request(
            "POST", f"/api/v1/orders/{order_id}/cancel", json={}, account_required=True
        )

    async def create_oco_order(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        take_profit_price: Decimal,
        stop_trigger_price: Decimal,
        stop_order_price: Decimal,
        expire_date: str,
    ) -> dict:
        payload = {
            "symbol": symbol,
            "type": "OCO",
            "quantity": format(quantity, "f"),
            "orderType": "LIMIT",
            "clientOrderId": client_order_id,
            "expireDate": expire_date,
            "first": {
                "orderSide": "SELL",
                "triggerPrice": format(take_profit_price, "f"),
                "orderPrice": format(take_profit_price, "f"),
            },
            "second": {
                "orderSide": "SELL",
                "triggerPrice": format(stop_trigger_price, "f"),
                "orderPrice": format(stop_order_price, "f"),
            },
        }
        data = await self._request(
            "POST", "/api/v1/conditional-orders", json=payload, account_required=True
        )
        result = data.get("result") or {}
        if not result.get("conditionalOrderId"):
            raise BrokerError("조건주문 응답에 conditionalOrderId가 없습니다.")
        return {**result, "request": payload, "response": data}

    async def conditional_order_detail(self, conditional_order_id: str) -> dict:
        data = await self._request(
            "GET",
            f"/api/v1/conditional-orders/{conditional_order_id}",
            account_required=True,
        )
        return data.get("result") or {}

    async def find_conditional_order(self, client_order_id: str, symbol: str) -> dict | None:
        for lifecycle in ("OPEN", "CLOSED"):
            data = await self._request(
                "GET",
                "/api/v1/conditional-orders",
                params={"status": lifecycle, "symbol": symbol, "limit": 100},
                account_required=True,
            )
            result = data.get("result") or {}
            rows = (
                result
                if isinstance(result, list)
                else result.get("conditionalOrders") or result.get("items") or []
            )
            for item in rows:
                if item.get("clientOrderId") == client_order_id:
                    return item
        return None

    async def cancel_conditional_order(self, conditional_order_id: str) -> None:
        await self._request(
            "DELETE",
            f"/api/v1/conditional-orders/{conditional_order_id}",
            account_required=True,
        )


class PaperBroker(Broker):
    """자격 증명 없이 전체 흐름을 검증하는 안전한 모의 브로커."""

    PRICES = {
        "005930": Decimal("72000"),
        "000660": Decimal("210000"),
        "035420": Decimal("185000"),
        "005380": Decimal("245000"),
        "068270": Decimal("170000"),
        "105560": Decimal("85000"),
        "AAPL": Decimal("200"),
        "MSFT": Decimal("480"),
        "NVDA": Decimal("150"),
        "GOOGL": Decimal("190"),
        "AMZN": Decimal("220"),
        "META": Decimal("650"),
        "BRK.B": Decimal("500"),
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    async def _ensure_cash(self, session) -> None:
        if await session.get(PaperCash, "KRW") is None:
            session.add(PaperCash(currency="KRW", amount=str(self.settings.paper_cash_krw)))
        if await session.get(PaperCash, "USD") is None:
            session.add(PaperCash(currency="USD", amount=str(self.settings.paper_cash_usd)))
        await session.flush()
        await self._rebuild_portfolio_from_old_trade_logs(session)

    def _display_name(self, symbol: str) -> str:
        return KR_STOCK_NAMES.get(symbol) or US_STOCK_NAMES.get(symbol) or symbol

    async def _rebuild_portfolio_from_old_trade_logs(self, session) -> None:
        """구버전 모의 매매기록을 새 모의 포트폴리오 테이블로 1회 복구한다.

        예전 버전은 매매기록만 남기고 paper_cash/paper_holdings를 갱신하지 않았다.
        이미 운영 중인 사용자가 업데이트하면 과거 모의 매수 기록은 있는데 보유 종목과
        현금이 그대로 보일 수 있으므로, 최초 1회 기존 PAPER_FILLED 로그를 재생한다.
        """

        rebuilt = await session.scalar(
            select(AuditLog.id)
            .where(AuditLog.event_type == "PAPER_PORTFOLIO_REBUILT")
            .limit(1)
        )
        if rebuilt:
            return

        existing_holding = await session.scalar(select(PaperHolding.symbol).limit(1))
        if existing_holding is not None:
            session.add(
                AuditLog(
                    event_type="PAPER_PORTFOLIO_REBUILT",
                    message="기존 모의 보유 종목이 있어 과거 매매기록 재계산을 건너뛰었습니다.",
                    details={"reason": "existing_paper_holdings"},
                )
            )
            await session.flush()
            return

        cash_krw = await session.get(PaperCash, "KRW")
        cash_usd = await session.get(PaperCash, "USD")
        if cash_krw is None or cash_usd is None:
            return

        initial_krw = Decimal(str(self.settings.paper_cash_krw))
        initial_usd = Decimal(str(self.settings.paper_cash_usd))
        if Decimal(cash_krw.amount) != initial_krw or Decimal(cash_usd.amount) != initial_usd:
            session.add(
                AuditLog(
                    event_type="PAPER_PORTFOLIO_REBUILT",
                    message="모의 현금이 이미 변경되어 과거 매매기록 재계산을 건너뛰었습니다.",
                    details={"reason": "paper_cash_already_changed"},
                )
            )
            await session.flush()
            return

        rows = (
            await session.scalars(
                select(TradeLog)
                .where(TradeLog.status == "PAPER_FILLED")
                .order_by(TradeLog.created_at, TradeLog.id)
            )
        ).all()
        if not rows:
            return

        replayed = 0
        for row in rows:
            symbol = row.symbol.upper()
            try:
                market = Market(row.market)
                quantity = Decimal(row.quantity)
                price = Decimal(row.price or "0")
            except Exception:
                continue
            if quantity <= 0 or price <= 0:
                continue

            currency = "KRW" if market == Market.KR else "USD"
            cash = cash_krw if currency == "KRW" else cash_usd
            holding = await session.get(PaperHolding, symbol)
            notional = quantity * price

            if row.side == Action.BUY.value:
                cash.amount = str(Decimal(cash.amount) - notional)
                if holding is None:
                    session.add(
                        PaperHolding(
                            symbol=symbol,
                            name=self._display_name(symbol),
                            market=market.value,
                            currency=currency,
                            quantity=str(quantity),
                            average_price=str(price),
                        )
                    )
                    await session.flush()
                else:
                    old_quantity = Decimal(holding.quantity)
                    old_average = Decimal(holding.average_price)
                    new_quantity = old_quantity + quantity
                    holding.quantity = str(new_quantity)
                    holding.average_price = str(
                        ((old_quantity * old_average) + (quantity * price)) / new_quantity
                    )
                replayed += 1
            elif row.side == Action.SELL.value:
                cash.amount = str(Decimal(cash.amount) + notional)
                if holding is not None:
                    old_quantity = Decimal(holding.quantity)
                    remaining = old_quantity - quantity
                    if remaining <= 0:
                        await session.delete(holding)
                    else:
                        holding.quantity = str(remaining)
                replayed += 1

        session.add(
            AuditLog(
                event_type="PAPER_PORTFOLIO_REBUILT",
                message=f"과거 모의 매매기록 {replayed}건을 보유 종목과 현금에 반영했습니다.",
                details={"replayed": replayed},
            )
        )
        await session.flush()

    async def account_snapshot(self) -> AccountSnapshot:
        async with SessionLocal() as session:
            await self._ensure_cash(session)
            cash_krw = Decimal((await session.get(PaperCash, "KRW")).amount)
            cash_usd = Decimal((await session.get(PaperCash, "USD")).amount)
            rows = (await session.scalars(select(PaperHolding))).all()
            holdings: list[Holding] = []
            for row in rows:
                quantity = Decimal(row.quantity)
                if quantity <= 0:
                    continue
                last_price = self.PRICES.get(row.symbol, Decimal(row.average_price))
                average_price = Decimal(row.average_price)
                market_value = quantity * last_price
                purchase_amount = quantity * average_price
                profit_loss = market_value - purchase_amount
                profit_rate = profit_loss / purchase_amount if purchase_amount > 0 else Decimal("0")
                holdings.append(
                    Holding(
                        symbol=row.symbol,
                        name=row.name,
                        market=Market(row.market),
                        currency=row.currency,
                        quantity=quantity,
                        last_price=last_price,
                        average_price=average_price,
                        market_value=market_value,
                        profit_loss=profit_loss,
                        profit_rate=profit_rate,
                    )
                )
            await session.commit()
        kr_value = sum((h.market_value for h in holdings if h.currency == "KRW"), Decimal("0"))
        us_value = sum((h.market_value for h in holdings if h.currency == "USD"), Decimal("0"))
        return AccountSnapshot(
            captured_at=datetime.now(timezone.utc),
            holdings=holdings,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            equity_krw=cash_krw + kr_value,
            equity_usd=cash_usd + us_value,
        )

    async def prices(self, symbols: list[str]) -> dict[str, Decimal]:
        return {symbol: self.PRICES[symbol] for symbol in symbols if symbol in self.PRICES}

    async def stock_info(self, symbols: list[str]) -> dict[str, StockInfo]:
        result = {}
        for symbol in symbols:
            kr = symbol.isdigit()
            result[symbol] = StockInfo(
                symbol=symbol,
                name=self._display_name(symbol),
                market_name="KOSPI" if kr else "NASDAQ",
                security_type="STOCK" if kr else "FOREIGN_STOCK",
                status="ACTIVE",
                currency="KRW" if kr else "USD",
            )
        return result

    async def warnings(self, symbol: str) -> list[str]:
        return []

    async def market_session(self, market: Market) -> MarketSession:
        now = datetime.now(KST)
        if market == Market.KR:
            if now.weekday() >= 5:
                return MarketSession.CLOSED
            current = now.time()
            if time(8, 0) <= current < time(9, 0):
                return MarketSession.PRE
            if time(9, 0) <= current < time(15, 30):
                return MarketSession.REGULAR
            if time(15, 30) <= current < time(20, 0):
                return MarketSession.AFTER
            return MarketSession.CLOSED
        # 모의모드의 미국장 시간은 단순화된 KST 범위이며 실제 모드는 공식 캘린더를 쓴다.
        current = now.time()
        if now.weekday() >= 5 and not (now.weekday() == 5 and current < time(7, 0)):
            return MarketSession.CLOSED
        if time(9, 0) <= current < time(16, 50):
            return MarketSession.DAY
        if time(17, 0) <= current < time(22, 30):
            return MarketSession.PRE
        if current >= time(22, 30) or current < time(5, 0):
            return MarketSession.REGULAR
        if time(5, 0) <= current < time(7, 0):
            return MarketSession.AFTER
        return MarketSession.CLOSED

    async def buying_power(self, currency: str) -> Decimal:
        async with SessionLocal() as session:
            await self._ensure_cash(session)
            cash = await session.get(PaperCash, currency)
            await session.commit()
            return Decimal(cash.amount) if cash is not None else Decimal("0")

    async def sellable_quantity(self, symbol: str) -> Decimal:
        async with SessionLocal() as session:
            holding = await session.get(PaperHolding, symbol.upper())
            return Decimal(holding.quantity) if holding is not None else Decimal("0")

    async def place_order(self, order: OrderRequest) -> OrderResult:
        order_id = f"paper-{uuid.uuid4().hex}"
        symbol = order.symbol.upper()
        currency = "KRW" if order.market == Market.KR else "USD"
        price = order.price or self.PRICES.get(symbol)
        if price is None:
            raise BrokerError(f"모의투자 가격을 찾을 수 없습니다: {symbol}")
        if order.order_amount is not None:
            if order.action != Action.BUY:
                raise BrokerError("모의투자 금액 주문은 매수만 지원합니다.")
            notional = order.order_amount
            quantity = (notional / price).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
        elif order.quantity is not None:
            quantity = order.quantity
            notional = quantity * price
        else:
            raise BrokerError("모의투자 주문 수량 또는 주문 금액이 필요합니다.")

        async with SessionLocal() as session:
            await self._ensure_cash(session)
            cash = await session.get(PaperCash, currency)
            holding = await session.get(PaperHolding, symbol)
            if order.action == Action.BUY:
                available = Decimal(cash.amount)
                if available < notional:
                    raise BrokerError("모의투자 현금이 부족합니다.")
                cash.amount = str(available - notional)
                if holding is None:
                    session.add(
                        PaperHolding(
                            symbol=symbol,
                            name=self._display_name(symbol),
                            market=order.market.value,
                            currency=currency,
                            quantity=str(quantity),
                            average_price=str(price),
                        )
                    )
                else:
                    old_quantity = Decimal(holding.quantity)
                    old_average = Decimal(holding.average_price)
                    new_quantity = old_quantity + quantity
                    new_average = (
                        (old_quantity * old_average) + (quantity * price)
                    ) / new_quantity
                    holding.quantity = str(new_quantity)
                    holding.average_price = str(new_average)
            elif order.action == Action.SELL:
                if holding is None:
                    raise BrokerError("모의투자 보유 종목이 없어 매도할 수 없습니다.")
                old_quantity = Decimal(holding.quantity)
                if old_quantity < quantity:
                    raise BrokerError("모의투자 매도 가능 수량이 부족합니다.")
                cash.amount = str(Decimal(cash.amount) + notional)
                remaining = old_quantity - quantity
                if remaining <= 0:
                    await session.delete(holding)
                else:
                    holding.quantity = str(remaining)
            await session.commit()

        return OrderResult(
            order_id=order_id,
            client_order_id=order.client_order_id,
            status="PAPER_FILLED",
            raw={
                "mode": "paper",
                "stock_name": self._display_name(symbol),
                "filledPrice": format(price, "f"),
                "filledQuantity": format(quantity, "f"),
                "filledAmount": format(notional, "f"),
                "currency": currency,
            },
        )

    async def order_detail(self, order_id: str) -> BrokerOrder:
        async with SessionLocal() as session:
            trade = await session.scalar(select(TradeLog).where(TradeLog.order_id == order_id))
        if trade is None:
            raise BrokerError(f"모의투자 주문을 찾을 수 없습니다: {order_id}")
        raw = trade.raw or {}
        return BrokerOrder(
            order_id=order_id,
            client_order_id=raw.get("client_order_id"),
            symbol=trade.symbol,
            side=trade.side,
            status=trade.status,
            order_type=raw.get("order_type"),
            quantity=Decimal(trade.quantity),
            price=Decimal(trade.price) if trade.price else None,
            filled_quantity=Decimal(raw.get("filledQuantity") or trade.quantity),
            average_filled_price=Decimal(raw.get("filledPrice") or trade.price or "0"),
            filled_amount=Decimal(raw.get("filledAmount") or "0"),
            raw=raw,
        )

    async def find_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        return None

    async def cancel_order(self, order_id: str) -> None:
        return None

    async def create_oco_order(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        take_profit_price: Decimal,
        stop_trigger_price: Decimal,
        stop_order_price: Decimal,
        expire_date: str,
    ) -> dict:
        return {
            "conditionalOrderId": f"paper-oco-{uuid.uuid4().hex}",
            "clientOrderId": client_order_id,
            "status": "WATCHING",
        }

    async def conditional_order_detail(self, conditional_order_id: str) -> dict:
        return {"conditionalOrderId": conditional_order_id, "status": "WATCHING"}

    async def find_conditional_order(self, client_order_id: str, symbol: str) -> dict | None:
        return None

    async def cancel_conditional_order(self, conditional_order_id: str) -> None:
        return None


def create_broker(settings: Settings) -> Broker:
    return TossBroker(settings) if settings.broker_mode == "toss" else PaperBroker(settings)
