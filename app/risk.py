from decimal import Decimal
from urllib.parse import urlparse

from app.config import Settings
from app.profiles import DEFAULT_PROFILE, ProfileLimits, limits_for_profile
from app.schemas import Action, MarketSession, RiskContext, RiskResult, TradeProposal

BLOCKING_WARNINGS = {
    "LIQUIDATION_TRADING",
    "OVERHEATED",
    "INVESTMENT_WARNING",
    "INVESTMENT_RISK",
    "STOCK_WARRANTS",
}
ALLOWED_SECURITY_TYPES = {
    "STOCK",
    "FOREIGN_STOCK",
    "DEPOSITARY_RECEIPT",
    "INFRASTRUCTURE_FUND",
    "REIT",
    "ETF",
    "FOREIGN_ETF",
}
ALLOWED_MARKETS = {"KOSPI", "KOSDAQ", "NYSE", "NASDAQ", "AMEX"}


class RiskManager:
    """AI 판단과 무관하게 모든 주문을 최종 차단하거나 승인하는 안전 게이트."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(
        self,
        proposal: TradeProposal,
        ctx: RiskContext,
        *,
        profile_key: str = DEFAULT_PROFILE,
        limits: ProfileLimits | None = None,
    ) -> RiskResult:
        profile_limits = limits or limits_for_profile(self.settings, profile_key)
        reasons: list[str] = []
        stock = ctx.stock

        if proposal.action == Action.HOLD:
            reasons.append("관망 제안은 주문 대상이 아닙니다.")
        if profile_limits.force_hold and proposal.action != Action.HOLD:
            reasons.append("현재 투자 성향이 홀드 모드라 신규 주문을 차단합니다.")
        if not ctx.market_open:
            reasons.append("정규장 또는 허용된 프리·애프터마켓이 열려 있지 않습니다.")
        if ctx.market_session in {MarketSession.PRE, MarketSession.AFTER, MarketSession.DAY}:
            if not ctx.extended_hours_enabled:
                reasons.append("프리·애프터 거래 허용 옵션이 꺼져 있습니다.")
            if ctx.order_type != "LIMIT":
                reasons.append("프리·애프터마켓과 데이마켓 주문은 지정가만 허용합니다.")
        if proposal.confidence < profile_limits.min_confidence:
            reasons.append("AI 확신도가 현재 투자 성향의 최소 기준보다 낮습니다.")
        if proposal.target_weight_pct > profile_limits.max_position_weight * 100:
            reasons.append("AI 목표 비중이 현재 투자 성향의 종목별 최대 비중을 초과합니다.")
        if proposal.risk_score > profile_limits.max_risk_score:
            reasons.append("위험 점수가 현재 투자 성향의 허용 범위를 초과합니다.")

        source_hosts = {
            urlparse(str(item.url)).hostname
            for item in proposal.evidence
            if item.url and urlparse(str(item.url)).hostname
        }

        if len(source_hosts) < 1:
            reasons.append("URL로 확인 가능한 객관적 출처가 없습니다.")
        if stock.status != "ACTIVE":
            reasons.append("상장 상태가 ACTIVE가 아닙니다.")
        if stock.market_name not in ALLOWED_MARKETS:
            reasons.append("허용된 거래 시장이 아닙니다.")
        if stock.security_type not in ALLOWED_SECURITY_TYPES:
            reasons.append("허용되지 않은 상품 유형입니다.")
        if stock.security_type in {"ETF", "FOREIGN_ETF"}:
            if stock.leverage_factor is not None and stock.leverage_factor != Decimal("1"):
                reasons.append("레버리지 또는 인버스 ETF는 금지합니다.")
        blocked = sorted(BLOCKING_WARNINGS.intersection(ctx.warnings))
        if blocked:
            reasons.append(f"매수·매도 유의 종목입니다: {', '.join(blocked)}")
        if ctx.daily_return <= -Decimal(str(profile_limits.max_daily_loss)):
            reasons.append("일일 최대 손실 한도에 도달했습니다.")
        if ctx.daily_order_count >= profile_limits.max_daily_orders:
            reasons.append("일일 최대 주문 횟수에 도달했습니다.")
        if ctx.proposed_quantity <= 0 or ctx.proposed_notional <= 0:
            reasons.append("주문 수량 또는 금액이 0 이하입니다.")
        if ctx.portfolio_equity <= 0:
            reasons.append("포트폴리오 평가금액을 확인할 수 없습니다.")

        if proposal.action == Action.BUY and ctx.portfolio_equity > 0:
            max_order = ctx.portfolio_equity * Decimal(str(profile_limits.max_order_weight))
            max_position = ctx.portfolio_equity * Decimal(str(profile_limits.max_position_weight))
            reserve = ctx.portfolio_equity * Decimal(str(profile_limits.min_cash_reserve))
            if ctx.proposed_notional > max_order:
                reasons.append("1회 주문 한도를 초과합니다.")
            if ctx.current_position_value + ctx.proposed_notional > max_position:
                reasons.append("종목별 최대 비중을 초과합니다.")
            if ctx.proposed_notional > ctx.buying_power:
                reasons.append("현금 매수 가능 금액을 초과합니다.")
            if ctx.buying_power - ctx.proposed_notional < reserve:
                reasons.append("최소 현금 보유 비중을 침해합니다.")

        if proposal.action == Action.SELL:
            if ctx.proposed_quantity > ctx.sellable_quantity:
                reasons.append("매도 가능 수량을 초과합니다.")
            if ctx.current_quantity <= 0:
                reasons.append("보유하지 않은 종목은 매도할 수 없습니다.")

        return RiskResult(approved=not reasons, reasons=reasons)
