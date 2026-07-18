from decimal import Decimal

from app.config import Settings
from app.risk import RiskManager
from app.schemas import (
    Action,
    Evidence,
    Market,
    RiskContext,
    StockInfo,
    TradeProposal,
)


def settings() -> Settings:
    return Settings(_env_file=None)


def proposal(**changes) -> TradeProposal:
    data = {
        "symbol": "005930",
        "market": Market.KR,
        "action": Action.BUY,
        "confidence": 0.9,
        "target_weight_pct": 10,
        "expected_return_pct": 12,
        "risk_score": 4,
        "thesis": "실적 개선과 현금흐름 증가가 확인되어 위험 대비 기대수익이 양호합니다.",
        "evidence": [
            Evidence(
                title="공식 실적 발표",
                url="https://company.example/earnings",
                fact="영업이익이 전년 대비 증가했습니다.",
            ),
            Evidence(
                title="공식 사업 보고서",
                url="https://dart.fss.or.kr/report",
                fact="현금흐름과 재무건전성이 개선됐습니다.",
            ),
        ],
    }
    data.update(changes)
    return TradeProposal(**data)


def context(**changes) -> RiskContext:
    data = {
        "market_open": True,
        "stock": StockInfo(
            symbol="005930",
            name="삼성전자",
            market_name="KOSPI",
            security_type="STOCK",
            status="ACTIVE",
            currency="KRW",
        ),
        "warnings": [],
        "buying_power": Decimal("5000000"),
        "sellable_quantity": Decimal("0"),
        "current_quantity": Decimal("0"),
        "current_position_value": Decimal("0"),
        "portfolio_equity": Decimal("10000000"),
        "daily_return": Decimal("0"),
        "daily_order_count": 0,
        "proposed_quantity": Decimal("4"),
        "proposed_notional": Decimal("288000"),
    }
    data.update(changes)
    return RiskContext(**data)


def test_valid_cash_buy_is_approved():
    result = RiskManager(settings()).evaluate(proposal(), context())
    assert result.approved
    assert result.reasons == []


def test_market_closed_is_rejected():
    result = RiskManager(settings()).evaluate(proposal(), context(market_open=False))
    assert not result.approved
    assert any("정규장" in reason for reason in result.reasons)


def test_leveraged_etf_is_rejected():
    stock = context().stock.model_copy(
        update={"security_type": "ETF", "leverage_factor": Decimal("2")}
    )
    result = RiskManager(settings()).evaluate(proposal(), context(stock=stock))
    assert not result.approved
    assert any("레버리지" in reason for reason in result.reasons)


def test_warning_stock_is_rejected():
    result = RiskManager(settings()).evaluate(
        proposal(), context(warnings=["INVESTMENT_WARNING"])
    )
    assert not result.approved
    assert any("유의 종목" in reason for reason in result.reasons)


def test_missing_evidence_is_rejected():
    p = proposal(evidence=[])
    result = RiskManager(settings()).evaluate(p, context())
    assert not result.approved
    assert any("출처" in reason for reason in result.reasons)


def test_daily_loss_circuit_is_rejected():
    result = RiskManager(settings()).evaluate(
        proposal(), context(daily_return=Decimal("-0.04"))
    )
    assert not result.approved
    assert any("일일 최대 손실" in reason for reason in result.reasons)


def test_sell_more_than_available_is_rejected():
    p = proposal(action=Action.SELL, target_weight_pct=0)
    result = RiskManager(settings()).evaluate(
        p,
        context(
            current_quantity=Decimal("10"),
            current_position_value=Decimal("720000"),
            sellable_quantity=Decimal("5"),
            proposed_quantity=Decimal("10"),
            proposed_notional=Decimal("720000"),
        ),
    )
    assert not result.approved
    assert any("매도 가능" in reason for reason in result.reasons)
