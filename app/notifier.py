import html
import re

import httpx

from app.ai import koreanize_ai_text
from app.config import Settings
from app.schemas import BrokerOrder, OrderRequest, OrderResult, TradeProposal

TELEGRAM_SAFE_LIMIT = 3600


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        token = self.settings.telegram_bot_token
        assert token is not None
        safe_text = text[:TELEGRAM_SAFE_LIMIT]
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": safe_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code == 400:
                plain_text = self._strip_html(safe_text)
                response = await client.post(
                    f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
                    json={
                        "chat_id": self.settings.telegram_chat_id,
                        "text": plain_text,
                        "disable_web_page_preview": True,
                    },
                )
            response.raise_for_status()

    @staticmethod
    def _strip_html(text: str) -> str:
        plain = re.sub(r"</?(b|code)>", "", text)
        return html.unescape(plain)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        cleaned = " ".join(str(text).split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1].rstrip() + "…"

    @staticmethod
    def _clean_reason(text: str) -> str:
        cleaned = re.sub(r"\[([^\]]{1,80})\]\(https?://[^\s)]+\)", r"\1", text)
        cleaned = re.sub(r"https?://[^\s<>)\"']+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return cleaned.strip()

    def format_trade_message(
        self,
        proposal: TradeProposal,
        order: OrderRequest,
        result: OrderResult,
        market_summary: str,
    ) -> str:
        evidence_lines = []
        for item in proposal.evidence[:3]:
            title = self._clip(koreanize_ai_text(item.title), 120)
            fact = self._clip(koreanize_ai_text(item.fact), 160)
            evidence_lines.append(f"• {html.escape(title)} — {html.escape(fact)}")

        market_name = "국내" if order.market.value == "KR" else "미국"
        action_name = "매수" if order.action.value == "BUY" else "매도"
        order_type_name = "시장가" if order.order_type == "MARKET" else "지정가"
        stock_name = result.raw.get("stock_name") or result.raw.get("name") or order.symbol
        status_name = {
            "SUBMITTED": "주문 접수",
            "PAPER_FILLED": "모의 주문 완료",
            "FILLED": "체결 완료",
        }.get(result.status, result.status)
        if order.order_amount is not None:
            order_description = f"USD {order.order_amount} 금액 주문 ({order_type_name})"
        else:
            order_description = f"{order.quantity}주 ({order_type_name})"
        return (
            "<b>AI 주식 투자 비서 주문 알림</b>\n"
            f"시장/종목: {market_name} / <b>{html.escape(str(stock_name))}</b>"
            f" ({html.escape(order.symbol)})\n"
            f"주문: <b>{action_name}</b> {order_description}\n"
            f"주문 ID: <code>{html.escape(result.order_id)}</code>\n"
            f"처리 결과: {html.escape(status_name)}\n"
            f"AI 확신 정도: {proposal.confidence:.0%}\n"
            f"예상 수익: {proposal.expected_return_pct:.1f}% / 위험도: {proposal.risk_score}/10\n"
            f"시장 판단: {html.escape(self._clip(koreanize_ai_text(market_summary), 450))}\n"
            f"판단 이유: {html.escape(self._clip(self._clean_reason(koreanize_ai_text(proposal.thesis)), 650))}\n"
            f"근거:\n{chr(10).join(evidence_lines) or '• 없음'}"
        )

    async def trade(
        self,
        proposal: TradeProposal,
        order: OrderRequest,
        result: OrderResult,
        market_summary: str,
    ) -> None:
        await self.send(self.format_trade_message(proposal, order, result, market_summary))

    async def order_status(self, intent, order: BrokerOrder) -> None:
        status_name = {
            "PENDING": "접수 대기",
            "PARTIAL_FILLED": "부분 체결",
            "FILLED": "체결 완료",
            "CANCELED": "주문 취소",
            "REJECTED": "주문 거절",
            "PENDING_CANCEL": "취소 처리 중",
        }.get(order.status, order.status)
        action_name = "매수" if intent.side == "BUY" else "매도"
        raw = intent.raw or {}
        stock_name = raw.get("stock_name") or raw.get("name") or intent.symbol
        price = (
            f"{order.average_filled_price}"
            if order.average_filled_price is not None
            else "-"
        )
        await self.send(
            "<b>토스 주문 상태 변경</b>\n"
            f"종목: <b>{html.escape(str(stock_name))}</b> ({html.escape(intent.symbol)})\n"
            f"구분: {action_name} / {html.escape(status_name)}\n"
            f"체결 수량: {order.filled_quantity}\n"
            f"평균 체결가: {price}\n"
            f"주문 ID: <code>{html.escape(order.order_id)}</code>"
        )

    async def protection_created(self, protection) -> None:
        raw = protection.raw or {}
        stock_name = raw.get("stock_name") or raw.get("name") or protection.symbol
        await self.send(
            "<b>OCO 손절·익절 보호주문 등록</b>\n"
            f"종목: <b>{html.escape(str(stock_name))}</b> ({html.escape(protection.symbol)})\n"
            f"보호 수량: {protection.quantity}\n"
            f"익절 감시가: {protection.take_profit_price}\n"
            f"손절 감시가: {protection.stop_trigger_price}\n"
            f"조건주문 ID: <code>{html.escape(protection.conditional_order_id or '-')}</code>"
        )

    async def protection_updated(self, protection, *, old_conditional_order_id: str) -> None:
        raw = protection.raw or {}
        stock_name = raw.get("stock_name") or raw.get("name") or protection.symbol
        await self.send(
            "<b>OCO 손절·익절 보호주문 갱신</b>\n"
            f"종목: <b>{html.escape(str(stock_name))}</b> ({html.escape(protection.symbol)})\n"
            f"보호 수량: {protection.quantity}\n"
            f"익절 감시가: {protection.take_profit_price}\n"
            f"손절 감시가: {protection.stop_trigger_price}\n"
            f"이전 조건주문 ID: <code>{html.escape(old_conditional_order_id)}</code>\n"
            f"새 조건주문 ID: <code>{html.escape(protection.conditional_order_id or '-')}</code>"
        )

    async def failure(self, reason: str, stopped: bool) -> None:
        title = "자동매매 중단" if stopped else "자동매매 오류"
        await self.send(f"<b>{title}</b>\n{html.escape(reason[:1500])}")
