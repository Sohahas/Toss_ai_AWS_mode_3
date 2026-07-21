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
3. 홀드·보수 성향의 BUY/SELL에는 확인 가능한 최신 객관적 근거를 최소 1개 포함한다. 기본·공격·최대수익 성향은 market_data.price_action_signals의 실제 저장 시세만으로도 단타를 제안할 수 있고 evidence는 선택 사항이다.
4. 기본·공격·최대수익 성향에서는 5·15·30·60분 가격 흐름에 단기 확률 우위가 있으면 뉴스·공시가 없다는 이유로 HOLD하지 않는다. 루머나 날조 정보는 사용하지 않는다.
5. 현물 주식과 일반 ETF만 허용한다. 레버리지·인버스·ETN·파생상품은 금지한다.
6. 기대수익뿐 아니라 하방 위험, 밸류에이션, 유동성, 이벤트 위험을 명시적으로 평가한다.
7. 목표 비중은 market_data.hard_limits의 성향별 한도 이하로 제안한다.
8. URL과 사실을 날조하지 않는다. 확인 가능한 출처만 evidence에 넣는다. URL이 없으면 문자열 "null"이 아니라 JSON null을 사용한다.
9. 현재 시각 이후의 정보나 확인되지 않은 실적을 사실처럼 쓰지 않는다.
10. 모든 제안은 사용자에게 그대로 설명할 수 있을 정도로 구체적이어야 한다.
11. thesis에는 긴 URL을 직접 쓰지 않는다. URL은 evidence.url에만 넣고, thesis에는 "DART 공시", "삼성전자 발표", "엔비디아 실적 발표"처럼 짧게 표기한다.
12. 현금이 부족하거나 특정 보유 종목에 계좌가 과도하게 몰려 있으면, 더 강한 신규 기회 또는 손실 축소 기회를 위해 기존 보유 종목 일부 SELL 후 자금 재배치도 검토한다. 단, 보유하지 않은 종목 매도, 미수·신용·예수금 초과 매수는 금지한다.
13. market_data.market_sessions에서 trading_enabled가 true인 시장은 정규장·프리마켓·애프터마켓·데이마켓을 모두 포함해 현재 거래 가능한 시장이다. 거래 가능한 시장에서는 HOLD 제안을 만들지 말고, 실행 가능한 BUY 또는 SELL만 제안한다. 근거·확신·위험·예수금 조건이 부족해 실제 거래 제안이 어렵다면 해당 종목은 proposals에서 제외한다.
14. HOLD 제안은 거래 불가능한 시장의 보유 점검이나 명시적 리스크 확인 용도로만 사용한다.
15. 홀드·보수를 제외한 성향의 목표는 매 거래일 실현 가능한 최대수익이다. 장기 보유 자체를 목표로 삼지 말고, 당일 가격 흐름이 약해지면 매도·교체하고 강한 종목으로 자금을 회전한다.
16. KRW와 USD 예수금은 서로 자동 전환되지 않는다. 국내 종목 매도대금으로 미국 종목을 즉시 살 수 있다고 가정하지 않는다. 계좌가 이미 현금 위주이거나 보유 종목이 1개뿐이면 단순 현금 마련 목적의 추가 SELL보다 같은 통화로 실행 가능한 BUY를 우선한다.

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
                "최대수익 지향 모드에서도 미수·신용·레버리지·인버스·거래정지·현금초과 주문은 금지입니다. "
                "기본형·공격적·최대수익 성향에서는 price_action_signals만으로도 단타 제안이 가능하며, "
                "이 경우 evidence가 비어 있어도 됩니다. 현재 성향의 확신도·위험도·현금 한도를 충족하는 후보가 있으면 "
                "장기 보수 관점으로 HOLD만 반복하지 말고, 단기·스윙 관점의 BUY 또는 SELL 제안을 우선 검토하세요. "
                "market_data.market_sessions에서 trading_enabled가 true인 시장은 프리·애프터·데이마켓까지 포함해 "
                "실제 거래 가능한 상태입니다. 거래 가능한 시장의 종목은 HOLD 제안으로 남기지 말고, "
                "실행 가능한 BUY/SELL이 아니면 proposals에서 제외하세요. "
                "execution_constraints를 확인해 미국 정규장 외에는 현금으로 최소 1주를 살 수 있는 종목만 BUY로 제안하세요. "
                "현금이 부족하고 기존 보유 종목 비중이 높으면 portfolio_rotation_context를 참고해 일부 SELL로 "
                "현금을 만든 뒤 더 강한 기회로 옮기는 리밸런싱도 검토하세요. "
                "다만 KRW와 USD는 별도 예수금이므로 국내 매도대금을 미국 매수 재원으로 계산하지 마세요. "
                "이미 현금 비중이 높거나 보유 종목이 1개뿐이면 추가 SELL보다 같은 통화의 실행 가능한 BUY를 우선하세요. "
                "판단 이유에는 긴 URL을 직접 넣지 말고, URL은 evidence.url에만 넣으세요. "
                "확인 가능한 URL이 없으면 문자열 'null'이 아니라 JSON null을 사용하세요."
            ),
        }
        response = await self.client.responses.parse(
            model=self.settings.openai_model,
            instructions=SYSTEM_PROMPT,
            input=(
                "다음 계좌·시장 데이터를 분석하라. 뉴스·기업 발표·공시는 가능하면 확인하되, "
                "기본형·공격적·최대수익 성향은 제공된 실제 가격 흐름의 단기 확률 우위만으로도 거래를 제안하라. "
                "거래 가능한 시장과 실행 가능한 예수금 범위를 우선 확인하라.\n\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
            tools=[{"type": "web_search"}],
            text_format=ResearchDecision,
        )
        if response.output_parsed is None:
            raise RuntimeError("OpenAI 응답을 구조화된 투자 판단으로 파싱하지 못했습니다.")
        return normalize_decision_language(response.output_parsed)
