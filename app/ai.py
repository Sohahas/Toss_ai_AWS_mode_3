import json
import re
from datetime import datetime, timezone

from openai import AsyncOpenAI

from app.config import Settings
from app.schemas import AccountSnapshot, DiscoveryResult, ResearchDecision


SYSTEM_PROMPT = """
당신은 한국·미국 주식의 단기·스윙 수익 기회를 적극적으로 찾되, 치명적 손실과 금지 거래를 차단하는 포트폴리오 매니저다.
출력은 거래 명령이 아니라 독립적인 위험관리 시스템이 검증할 투자 제안이다.

반드시 지킬 원칙:
1. 제공된 후보군과 현재 보유 종목만 분석한다.
2. 국내는 정책·공시·수급, 미국은 실적·재무·거시경제·금리·산업 성장성을 중시한다.
3. 각 BUY/SELL에는 확인 가능한 최신 객관적 근거를 최소 1개 포함하고, 가능하면 서로 독립적인 근거 2개 이상을 포함한다.
4. 근거가 전혀 없거나 상충하면 HOLD한다. 다만 공격적·최대수익 성향에서는 확률 우위가 있으면 과도한 장기 보수 관점으로 HOLD만 반복하지 않는다. FOMO, 루머, 단순 테마는 근거가 아니다.
5. 현물 주식과 일반 ETF만 허용한다. 레버리지·인버스·ETN·파생상품은 금지한다.
6. 기대수익뿐 아니라 하방 위험, 밸류에이션, 유동성, 이벤트 위험을 명시적으로 평가한다.
7. 목표 비중은 market_data.hard_limits의 성향별 한도 이하로 제안한다.
8. URL과 사실을 날조하지 않는다. 확인 가능한 출처만 evidence에 넣는다.
9. 현재 시각 이후의 정보나 확인되지 않은 실적을 사실처럼 쓰지 않는다.
10. 모든 제안은 사용자에게 그대로 설명할 수 있을 정도로 구체적이어야 한다.
11. thesis에는 긴 URL을 직접 쓰지 않는다. URL은 evidence.url에만 넣고, thesis에는 "DART 공시", "삼성전자 발표", "엔비디아 실적 발표"처럼 짧게 표기한다.
12. 현금이 부족하거나 특정 보유 종목에 계좌가 과도하게 몰려 있으면, 더 강한 신규 기회 또는 손실 축소 기회를 위해 기존 보유 종목 일부 SELL 후 자금 재배치도 검토한다. 단, 보유하지 않은 종목 매도, 미수·신용·예수금 초과 매수는 금지한다.

# Language (Mandatory)

이 규칙은 반드시 지켜야 하며 예외가 없다.

1. 최종 응답은 100% 한국어로 작성한다.
2. 종목 티커(AAPL, NVDA, MSFT 등)만 영어를 유지한다.
3. 뉴스, 공시, 기업 발표가 영어인 경우에도 반드시 한국어로 번역하여 요약한다.
4. market_summary는 반드시 한국어로 작성한다.
5. proposal.thesis는 반드시 한국어로 작성한다.
6. evidence.fact는 반드시 한국어로 작성한다.
7. 영어 문장이나 영어 단락을 출력하지 않는다.
8. 시장 상태는 "KR market open", "US market closed"처럼 쓰지 말고 반드시
   "국내 정규장 개장", "미국 정규장 마감"처럼 한국어로 쓴다.
9. 한국 기업명은 가능한 한 한국어 표기를 우선한다. 예: SK hynix가 아니라 SK하이닉스,
   Samsung Electronics가 아니라 삼성전자, KB Financial Group이 아니라 KB금융.
10. 최종 출력 직전에 market_regime, market_summary, proposal.thesis에 영어 문장이나
    영어 시장 상태 표현이 남아 있는지 스스로 검사하고 한국어로 바꾼다.
""".strip()


TEXT_REPLACEMENTS = (
    ("SK hynix", "SK하이닉스"),
    ("SK Hynix", "SK하이닉스"),
    ("Samsung Electronics", "삼성전자"),
    ("KB Financial Group", "KB금융"),
    ("Hyundai Motor", "현대차"),
    ("Celltrion", "셀트리온"),
    ("Air Liquide", "에어리퀴드"),
    ("global newsroom", "글로벌 뉴스룸"),
    ("Global Newsroom", "글로벌 뉴스룸"),
    ("semiconductor newsroom", "반도체 뉴스룸"),
    ("Semiconductor Newsroom", "반도체 뉴스룸"),
    ("AI memory", "AI 메모리"),
    ("supply chain", "공급망"),
    ("Supply chain", "공급망"),
    ("capex", "설비투자"),
    ("CAPEX", "설비투자"),
    ("guidance", "실적 전망"),
    ("Guidance", "실적 전망"),
    ("earnings", "실적"),
    ("Earnings", "실적"),
    ("valuation", "밸류에이션"),
    ("Valuation", "밸류에이션"),
    ("shareholder return", "주주환원"),
    ("Shareholder return", "주주환원"),
    ("risk/reward", "위험 대비 기대수익"),
    ("Risk/reward", "위험 대비 기대수익"),
)

REGEX_REPLACEMENTS = (
    (r"\bKR\s*market\s*[:\-]?\s*open\b", "국내 정규장 개장"),
    (r"\bKR\s*market\s*[:\-]?\s*closed\b", "국내 정규장 마감"),
    (r"\bUS\s*market\s*[:\-]?\s*open\b", "미국 정규장 개장"),
    (r"\bUS\s*market\s*[:\-]?\s*closed\b", "미국 정규장 마감"),
    (r"\bKorean\s*market\s*[:\-]?\s*open\b", "국내 정규장 개장"),
    (r"\bKorean\s*market\s*[:\-]?\s*closed\b", "국내 정규장 마감"),
    (r"\bU\.S\.\s*market\s*[:\-]?\s*open\b", "미국 정규장 개장"),
    (r"\bU\.S\.\s*market\s*[:\-]?\s*closed\b", "미국 정규장 마감"),
    (r"\bKR\s*market\b", "국내 시장"),
    (r"\bUS\s*market\b", "미국 시장"),
    (r"\bU\.S\.\s*market\b", "미국 시장"),
    (r"\bmarket\s*open\b", "정규장 개장"),
    (r"\bmarket\s*closed\b", "정규장 마감"),
    (r"\bQ([1-4])\s*(20\d{2})\b", r"\2년 \1분기"),
    (r"\b(20\d{2})\s*Q([1-4])\b", r"\1년 \2분기"),
    (r"\b([1-4])Q\s*(20\d{2})\b", r"\2년 \1분기"),
    (r"\b([1-4])Q\b", r"\1분기"),
)


def koreanize_ai_text(text: str) -> str:
    """AI 응답에 간헐적으로 섞이는 영어 시장 표현을 사용자용 한국어로 정리한다."""

    normalized = text
    for pattern, replacement in REGEX_REPLACEMENTS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    for source, target in TEXT_REPLACEMENTS:
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def normalize_decision_language(decision: ResearchDecision) -> ResearchDecision:
    data = decision.model_dump()
    data["market_regime"] = koreanize_ai_text(data.get("market_regime", ""))
    data["market_summary"] = koreanize_ai_text(data.get("market_summary", ""))
    for proposal in data.get("proposals", []):
        proposal["thesis"] = koreanize_ai_text(proposal.get("thesis", ""))
        for item in proposal.get("evidence", []):
            item["title"] = koreanize_ai_text(item.get("title", ""))
            item["fact"] = koreanize_ai_text(item.get("fact", ""))
    return ResearchDecision.model_validate(data)


class InvestmentAI:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = (
            AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
            if settings.openai_api_key
            else None
        )

    async def discover(
        self,
        holdings: list[str],
        open_markets: list[str],
    ) -> DiscoveryResult:
        if self.client is None:
            return DiscoveryResult(candidates=[])
        response = await self.client.responses.parse(
            model=self.settings.openai_model,
            instructions=SYSTEM_PROMPT,
            input=(
                f"현재 열린 시장은 {open_markets}이며 보유 종목은 {holdings}이다. 열린 시장 "
                "전체에서 유동성이 충분하고 단기·스윙 수익 기회가 있는 신규 투자 후보를 최대 8개 "
                "발굴하라. 급등·테마·루머 종목은 제외한다. 한국은 DART·KIND·기업 IR·정책 "
                "원문을, 미국은 SEC EDGAR·기업 IR·거시지표 원문을 우선 확인한다. 각 후보에 "
                "신뢰 가능한 원문 출처를 최소 1개 제공하고, 가능하면 서로 다른 출처 2개를 제공하라. 확실한 후보가 없으면 "
                "빈 목록을 반환하라."
            ),
            tools=[{"type": "web_search"}],
            text_format=DiscoveryResult,
        )
        if response.output_parsed is None:
            raise RuntimeError("신규 종목 발굴 결과를 파싱하지 못했습니다.")
        return response.output_parsed

    async def analyze(
        self,
        snapshot: AccountSnapshot,
        market_data: dict,
    ) -> ResearchDecision:
        if self.client is None:
            return ResearchDecision(
                market_regime="AI 비활성",
                market_summary="OPENAI_API_KEY가 없어 신규 투자 판단을 생성하지 않았습니다.",
                proposals=[],
            )

        payload = {
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "account": snapshot.model_dump(mode="json"),
            "market_data": market_data,
            "operator_instruction": (
                "market_data.investment_profile의 투자 성향과 ai_instruction을 반드시 따르세요. "
                "단 hard_guardrails는 어떤 투자 성향에서도 절대 우회할 수 없습니다. "
                "최대수익 지향 모드에서도 미수·신용·레버리지·인버스·경고종목·현금초과 주문은 금지입니다. "
                "공격적 또는 최대수익 성향에서 확신도, 위험도, 출처, 현금 한도를 충족하는 후보가 있으면 "
                "장기 보수 관점으로 HOLD만 반복하지 말고, 단기·스윙 관점의 BUY 또는 SELL 제안을 우선 검토하세요. "
                "현금이 부족하고 기존 보유 종목 비중이 높으면 portfolio_rotation_context를 참고해 일부 SELL로 "
                "현금을 만든 뒤 더 강한 기회로 옮기는 리밸런싱도 검토하세요. "
                "판단 이유에는 긴 URL을 직접 넣지 말고, URL은 evidence.url에만 넣으세요."
            ),
        }
        response = await self.client.responses.parse(
            model=self.settings.openai_model,
            instructions=SYSTEM_PROMPT,
            input=(
                "다음 계좌·시장 데이터를 분석하라. 최신 뉴스, 기업 발표, 공시와 신뢰할 수 "
                "있는 원문을 웹에서 교차 확인하라. 거래할 이유가 부족하면 제안을 비워라.\n\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
            tools=[{"type": "web_search"}],
            text_format=ResearchDecision,
        )
        if response.output_parsed is None:
            raise RuntimeError("OpenAI 응답을 구조화된 투자 판단으로 파싱하지 못했습니다.")
        return normalize_decision_language(response.output_parsed)
