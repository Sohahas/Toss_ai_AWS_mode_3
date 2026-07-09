from dataclasses import asdict, dataclass

from app.config import Settings


DEFAULT_PROFILE = "balanced"


@dataclass(frozen=True)
class TradingProfile:
    key: str
    label: str
    short_label: str
    description: str
    ai_instruction: str


@dataclass(frozen=True)
class ProfileLimits:
    min_confidence: float
    max_position_weight: float
    max_order_weight: float
    min_cash_reserve: float
    max_daily_loss: float
    max_daily_orders: int
    max_risk_score: int
    cooldown_hours: int
    force_hold: bool = False

    def pct_payload(self) -> dict:
        return {
            "min_confidence_pct": round(self.min_confidence * 100, 2),
            "max_position_weight_pct": round(self.max_position_weight * 100, 2),
            "max_order_weight_pct": round(self.max_order_weight * 100, 2),
            "min_cash_reserve_pct": round(self.min_cash_reserve * 100, 2),
            "max_daily_loss_pct": round(self.max_daily_loss * 100, 2),
            "max_daily_orders": self.max_daily_orders,
            "max_risk_score": self.max_risk_score,
            "cooldown_hours": self.cooldown_hours,
            "force_hold": self.force_hold,
        }


PROFILE_DEFINITIONS: dict[str, TradingProfile] = {
    "hold": TradingProfile(
        key="hold",
        label="홀드",
        short_label="홀드",
        description="신규 매수·매도를 막고 계좌와 시장만 관찰합니다.",
        ai_instruction=(
            "현재 투자 성향은 홀드입니다. 신규 매수와 매도 제안은 하지 말고, "
            "보유 종목과 현금 비중을 점검한 뒤 HOLD만 제안하세요."
        ),
    ),
    "conservative": TradingProfile(
        key="conservative",
        label="보수적",
        short_label="보수",
        description="확실한 근거와 낮은 변동성을 우선합니다. 주문 크기와 종목 비중을 작게 유지합니다.",
        ai_instruction=(
            "현재 투자 성향은 보수적입니다. 원금 방어와 변동성 축소를 우선하고, "
            "근거가 아주 강한 경우에만 낮은 비중으로 매수하세요."
        ),
    ),
    "balanced": TradingProfile(
        key="balanced",
        label="기본형 · 보수적 수익추구",
        short_label="기본",
        description="기본값입니다. 원금 방어를 우선하되, 근거가 강한 수익 기회는 놓치지 않습니다.",
        ai_instruction=(
            "현재 투자 성향은 기본형·보수적 수익추구입니다. 원금 방어와 현금 여력을 우선하되, "
            "실적·공시·수급·시장 모멘텀이 모두 확인되는 수익 기회는 적극적으로 검토하세요."
        ),
    ),
    "aggressive": TradingProfile(
        key="aggressive",
        label="공격적",
        short_label="공격",
        description="수익 기회를 더 적극적으로 잡습니다. 단, 안전장치는 그대로 유지합니다.",
        ai_instruction=(
            "현재 투자 성향은 공격적입니다. 검증된 상승 모멘텀과 실적·공시 근거가 있으면 "
            "균형형보다 더 적극적으로 매수/비중확대를 제안하세요. 확신도·위험도·출처 기준을 충족하는 "
            "후보가 있으면 HOLD만 반복하지 말고 BUY 또는 SELL 제안을 우선 검토하세요."
        ),
    ),
    "max_return": TradingProfile(
        key="max_return",
        label="초공격형 · 최대수익 지향",
        short_label="최대수익",
        description=(
            "가장 높은 수익률을 노리는 모드입니다. 변동성과 손실 가능성이 가장 큽니다. "
            "일반 사용은 권장하지 않습니다. 불법·금지상품·경고종목·미수거래·현금초과 주문은 여전히 차단됩니다."
        ),
        ai_instruction=(
            "현재 투자 성향은 초공격형·최대수익 지향입니다. 합법적인 범위와 시스템 안전장치 안에서 "
            "기대수익이 가장 큰 후보를 우선 탐색하고, 강한 모멘텀·실적·공시·수급 근거가 확인되면 "
            "더 높은 목표 비중을 제안할 수 있습니다. 다만 금지상품, 경고종목, 출처 부족, 현금초과, "
            "미수거래 같은 하드 가드레일은 절대 우회하지 마세요. 기준을 충족하는 후보가 있다면 "
            "관망보다 실행 가능한 BUY 또는 SELL 제안을 우선하세요."
        ),
    ),
}


PROFILE_LIMITS: dict[str, ProfileLimits] = {
    "hold": ProfileLimits(
        min_confidence=1.0,
        max_position_weight=0.0,
        max_order_weight=0.0,
        min_cash_reserve=1.0,
        max_daily_loss=0.01,
        max_daily_orders=0,
        max_risk_score=1,
        cooldown_hours=24,
        force_hold=True,
    ),
    "conservative": ProfileLimits(
        min_confidence=0.85,
        max_position_weight=0.10,
        max_order_weight=0.03,
        min_cash_reserve=0.30,
        max_daily_loss=0.02,
        max_daily_orders=4,
        max_risk_score=5,
        cooldown_hours=12,
    ),
    "balanced": ProfileLimits(
        min_confidence=0.78,
        max_position_weight=0.15,
        max_order_weight=0.05,
        min_cash_reserve=0.20,
        max_daily_loss=0.03,
        max_daily_orders=8,
        max_risk_score=7,
        cooldown_hours=6,
    ),
    "aggressive": ProfileLimits(
        min_confidence=0.66,
        max_position_weight=0.30,
        max_order_weight=0.12,
        min_cash_reserve=0.05,
        max_daily_loss=0.05,
        max_daily_orders=18,
        max_risk_score=9,
        cooldown_hours=1,
    ),
    "max_return": ProfileLimits(
        min_confidence=0.55,
        max_position_weight=0.45,
        max_order_weight=0.18,
        min_cash_reserve=0.00,
        max_daily_loss=0.10,
        max_daily_orders=30,
        max_risk_score=10,
        cooldown_hours=0,
    ),
}


def normalize_profile_key(value: str | None) -> str:
    if value in PROFILE_DEFINITIONS:
        return str(value)
    return DEFAULT_PROFILE


def get_profile(value: str | None) -> TradingProfile:
    return PROFILE_DEFINITIONS[normalize_profile_key(value)]


def limits_for_profile(settings: Settings, value: str | None) -> ProfileLimits:
    profile_key = normalize_profile_key(value)
    limits = PROFILE_LIMITS[profile_key]
    if profile_key != DEFAULT_PROFILE:
        return limits
    return ProfileLimits(
        min_confidence=settings.min_confidence,
        max_position_weight=settings.max_position_weight,
        max_order_weight=settings.max_order_weight,
        min_cash_reserve=settings.min_cash_reserve,
        max_daily_loss=settings.max_daily_loss,
        max_daily_orders=settings.max_daily_orders,
        max_risk_score=limits.max_risk_score,
        cooldown_hours=limits.cooldown_hours,
        force_hold=limits.force_hold,
    )


def profile_options() -> list[dict]:
    return [
        {
            **asdict(profile),
            "limits": PROFILE_LIMITS[profile.key].pct_payload(),
        }
        for profile in PROFILE_DEFINITIONS.values()
    ]


def profile_ai_context(settings: Settings, value: str | None) -> dict:
    profile = get_profile(value)
    limits = limits_for_profile(settings, profile.key)
    return {
        "key": profile.key,
        "label": profile.label,
        "description": profile.description,
        "ai_instruction": profile.ai_instruction,
        "limits": limits.pct_payload(),
        "hard_guardrails": [
            "정규장 또는 사용자가 허용한 프리·애프터마켓에서만 거래",
            "허용된 일반 주식·일반 ETF만 거래",
            "현금 주문만 허용하고 미수·신용 거래 금지",
            "레버리지·인버스·ETN·파생상품 금지",
            "투자경고·위험·과열 등 차단 대상 종목 금지",
            "매수·매도 제안마다 서로 다른 신뢰 가능한 출처 2개 이상 필요",
            "현금·매도가능 수량·일일 손실·일일 주문 한도 초과 금지",
        ],
    }
