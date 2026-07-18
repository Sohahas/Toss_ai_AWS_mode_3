from decimal import Decimal

from app.ai import koreanize_ai_text
from app.config import Settings
from app.notifier import TELEGRAM_SAFE_LIMIT, TelegramNotifier
from app.schemas import Action, Evidence, Market, OrderRequest, OrderResult, TradeProposal


def test_koreanize_ai_text_replaces_common_english_market_phrases():
    text = (
        "KR market open, US market closed. "
        "SK hynix(000660)는 Q1 2026 earnings와 supply chain 모멘텀이 있습니다."
    )

    result = koreanize_ai_text(text)

    assert "KR market" not in result
    assert "US market" not in result
    assert "국내 정규장 개장" in result
    assert "미국 정규장 마감" in result
    assert "SK하이닉스(000660)" in result
    assert "2026년 1분기" in result
    assert "공급망" in result


def test_us_trade_telegram_message_is_korean_and_short_enough():
    notifier = TelegramNotifier(Settings())
    proposal = TradeProposal(
        symbol="AAPL",
        market=Market.US,
        action=Action.BUY,
        confidence=0.86,
        target_weight_pct=5,
        expected_return_pct=8.2,
        risk_score=4,
        thesis="US market open 상황에서 earnings와 valuation을 확인했습니다. " * 30,
        evidence=[
            Evidence(
                title="Apple earnings update " + ("very long title " * 8),
                url="https://example.com/a/very/long/source/url/for/us/stocks",
                published_at="2026-07-07",
                fact="공식 실적 발표와 현금흐름 개선이 확인됩니다. " * 12,
            )
            for _ in range(6)
        ],
    )
    order = OrderRequest(
        symbol="AAPL",
        market=Market.US,
        action=Action.BUY,
        order_amount=Decimal("50"),
        order_type="MARKET",
        client_order_id="test-us-order",
    )
    result = OrderResult(
        order_id="us-order-1",
        client_order_id="test-us-order",
        status="SUBMITTED",
        raw={"stock_name": "Apple"},
    )

    message = notifier.format_trade_message(
        proposal,
        order,
        result,
        "US market open, KR market closed. 미국 기술주 실적을 점검했습니다." * 20,
    )

    assert "미국" in message
    assert "US market" not in message
    assert "Apple" in message
    assert "AAPL" in message
    assert "USD 50 금액 주문" in message
    assert len(message) < TELEGRAM_SAFE_LIMIT
