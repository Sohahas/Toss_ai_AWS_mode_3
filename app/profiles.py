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
        label="기본형 · 실전 수익추구",
        short_label="기본",
        description="기본값입니다. 치명적 손실은 피하되, 단기·스윙 수익 기회를 적극적으로 잡습니다.",
        ai_instruction=(
            "현재 투자 성향은 기본형·실전 수익추구입니다. 치명적 손실과 금지 거래는 피하되, "
            "매 거래일 실현 가능한 최대수익을 목표로 하고, "
            "실적·공시뿐 아니라 저장된 5·15·30·60분 가격 흐름에서 확률 우위가 확인되면 "
            "별도 뉴스 URL이 없어도 단타·스윙 매수·매도를 적극적으로 검토하세요. "
            "현금이 부족하고 특정 보유 종목 비중이 과도하면, 일부 매도 후 더 강한 기회로 옮기는 리밸런싱도 검토하세요."
        ),
    ),
    "aggressive": TradingProfile(
        key="aggressive",
        label="공격적",
        short_label="공격",
        description="수익 기회를 더 적극적으로 잡습니다. 단, 안전장치는 그대로 유지합니다.",
        ai_instruction=(
            "현재 투자 성향은 공격적이며 매 거래일 실현 가능한 최대수익이 목표입니다. 저장된 5·15·30·60분 가격 흐름, 장 상태, 수급성 모멘텀에 "
            "단기 확률 우위가 있으면 뉴스·공시 URL이 없어도 균형형보다 더 적극적으로 매수/비중확대를 제안하세요. "
            "확신도·위험도·주문 한도를 충족하는 "
            "후보가 있으면 HOLD만 반복하지 말고 BUY 또는 SELL 제안을 우선 검토하세요. "
            "현금 여력이 부족하면 과집중 보유 종목을 줄여 신규 기회로 자금을 이동하는 제안을 적극 검토하세요."
        ),
    ),
    "max_return": TradingProfile(
        key="max_return",
        label="초공격형 · 최대수익 지향",
        short_label="최대수익",
        description=(
            "뉴스·공시가 없어도 장중 가격 흐름으로 단타를 허용하는 최고위험 모드입니다. "
            "일반 사용은 권장하지 않습니다. 미수·금지상품·거래정지·예수금 초과 주문은 여전히 차단됩니다."
        ),
        ai_instruction=(
            "현재 투자 성향은 초공격형·최대수익 지향이며 매 거래일 실현 가능한 최대수익이 최우선 목표입니다. 뉴스·공시 URL이 없더라도 "
            "market_data.price_action_signals의 5·15·30·60분 가격 변화와 장 상태에 단기 확률 우위가 있으면 "
            "장중 단타 BUY 또는 SELL을 적극 제안하세요. 객관적 출처는 있으면 사용하되 필수 조건이 아닙니다. "
            "거래 가능한 장에서 현금이 있으면 실행 가능한 목표 비중을 제안하고, 현금이 부족하면 약한 보유 종목을 "
            "일부 또는 전부 SELL해 더 강한 종목으로 회전하는 방안을 우선 검토하세요. 확신도·종목 비중·1회 주문·"
            "현금 보유·일일 손실·쿨다운 같은 소프트 제한은 이 모드에서 거래를 막는 이유로 사용하지 마세요. "
            "단, 미수·신용·예수금 초과, 보유수량 초과 매도, 금지상품, 거래정지, 중복·불명 주문 방지 장치는 절대 우회하지 마세요."
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
        min_confidence=0.55,
        max_position_weight=0.60,
        max_order_weight=0.60,
        min_cash_reserve=0.05,
        max_daily_loss=0.08,
        max_daily_orders=30,
        max_risk_score=8,
        cooldown_hours=0,
    ),
    "aggressive": ProfileLimits(
        min_confidence=0.35,
        max_position_weight=0.85,
        max_order_weight=0.85,
        min_cash_reserve=0.00,
        max_daily_loss=0.20,
        max_daily_orders=80,
        max_risk_score=10,
        cooldown_hours=0,
    ),
    "max_return": ProfileLimits(
        min_confidence=0.0,
        max_position_weight=1.0,
        max_order_weight=1.0,
        min_cash_reserve=0.00,
        max_daily_loss=1.0,
        max_daily_orders=200,
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
    # 행동패턴을 대시보드에서 바꾸면 즉시 같은 기준이 적용되어야 하므로,
    # 과거 .env의 보수적인 기본값이 balanced 성향을 덮어쓰지 않게 한다.
    return PROFILE_LIMITS[profile_key]


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
            (
                "홀드·보수 모드는 매수·매도 제안마다 신뢰 가능한 출처 1개 이상 필요; "
                "기본·공격·최대수익 모드는 저장된 장중 가격 흐름만으로도 단타 허용"
            ),
            "현금·매도가능 수량·일일 손실·일일 주문 한도 초과 금지",
        ],
    }
