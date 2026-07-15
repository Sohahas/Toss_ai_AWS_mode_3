import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import koreanize_ai_text
from app.broker import KR_STOCK_NAMES, US_STOCK_NAMES, create_broker, friendly_error_message
from app.config import Settings, get_settings
from app.db import (
    AuditLog,
    DecisionLog,
    PortfolioSnapshot,
    TradeLog,
    add_portfolio_snapshot,
    audit,
    engine,
    get_session,
    get_state,
    init_db,
)
from app.profiles import (
    DEFAULT_PROFILE,
    PROFILE_DEFINITIONS,
    get_profile,
    limits_for_profile,
    profile_options,
)

settings = get_settings()
security = HTTPBasic()
TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"
DECISIONS_TEMPLATE = Path(__file__).parent / "templates" / "decisions.html"
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,80})\]\((https?://[^\s)]+)\)")
URL_RE = re.compile(r"https?://[^\s<>)\"']+")


def display_stock_name(symbol: str, raw: dict | None = None) -> str:
    raw = raw or {}
    normalized_symbol = symbol.upper()
    for key in ("stock_name", "name"):
        value = raw.get(key)
        if value and str(value).upper() != normalized_symbol:
            return str(value)
    return (
        KR_STOCK_NAMES.get(normalized_symbol)
        or US_STOCK_NAMES.get(normalized_symbol)
        or symbol
    )


def stock_name_map(account: dict | None = None) -> dict[str, str]:
    names: dict[str, str] = {
        **{key.upper(): value for key, value in KR_STOCK_NAMES.items()},
        **{key.upper(): value for key, value in US_STOCK_NAMES.items()},
    }
    for item in (account or {}).get("holdings") or []:
        symbol = str(item.get("symbol", "")).upper()
        name = str(item.get("name") or "").strip()
        if symbol and name and name.upper() != symbol:
            names[symbol] = name
    return names


def replace_symbol_mentions(text: str, names: dict[str, str]) -> str:
    normalized = text
    for symbol, name in sorted(names.items(), key=lambda item: len(item[0]), reverse=True):
        if not symbol or not name or name.upper() == symbol:
            continue
        pattern = rf"(?<![A-Za-z0-9(]){re.escape(symbol)}(?![A-Za-z0-9])"
        normalized = re.sub(pattern, f"{name}({symbol})", normalized)
    return normalized


def clean_empty_parentheses(text: str) -> str:
    cleaned = re.sub(r"\(\s*\)", "", text)
    cleaned = re.sub(r"（\s*）", "", cleaned)
    cleaned = re.sub(r"\s+([,.])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def source_label(url: str, fallback: str | None = None) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "naver.com" in host:
        return "네이버 뉴스"
    if "dart.fss.or.kr" in host:
        return "DART 공시"
    if "kind.krx.co.kr" in host:
        return "KIND 공시"
    if "sec.gov" in host:
        return "SEC 공시"
    if "krx.co.kr" in host:
        return "한국거래소"
    if "samsung" in host:
        return "삼성전자"
    if "skhynix" in host:
        return "SK하이닉스"
    if fallback:
        return fallback[:30]
    return host.replace("www.", "") or "출처"


def split_source_links(text: str) -> tuple[str, list[dict]]:
    links: list[dict] = []
    seen: set[str] = set()

    def add_link(url: str, label: str | None = None) -> None:
        clean_url = url.rstrip(".,;)]}”’\"'")
        if clean_url in seen:
            return
        seen.add(clean_url)
        links.append({"label": source_label(clean_url, label), "url": clean_url})

    def replace_markdown(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        add_link(url, label)
        return ""

    without_markdown = MARKDOWN_LINK_RE.sub(replace_markdown, text)

    def replace_url(match: re.Match) -> str:
        add_link(match.group(0))
        return ""

    cleaned = URL_RE.sub(replace_url, without_markdown)
    cleaned = clean_empty_parentheses(cleaned)
    return cleaned, links


def merge_source_links(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append({"label": item.get("label") or source_label(url), "url": url})
    return merged[:8]


def evidence_source_links(evidence: list | None) -> list[dict]:
    links: list[dict] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        links.append(
            {
                "label": source_label(str(url), item.get("title") or item.get("source")),
                "url": str(url),
            }
        )
    return merge_source_links(links)


def holdings_by_symbol(account: dict | None) -> dict[str, dict]:
    holdings = (account or {}).get("holdings") or []
    return {
        str(item.get("symbol", "")).upper(): item
        for item in holdings
        if item.get("symbol")
    }


def account_matches_mode(account: dict | None, mode: str) -> bool:
    if not account:
        return False
    return account.get("_broker_mode") == mode


def broker_wait_message(config: Settings) -> str:
    if config.broker_mode == "toss" and not config.broker_api_enabled:
        return "AWS 주문봇이 아직 실계좌 정보를 DB에 올리지 않았습니다. AWS의 ai-stock-worker 상태와 DATABASE_URL을 확인해 주세요."
    return "계좌 정보를 아직 읽지 못했습니다."


def account_payload(snapshot, mode: str, captured_by: str) -> dict:
    payload = snapshot.model_dump(mode="json")
    payload["_broker_mode"] = mode
    payload["_captured_by"] = captured_by
    return payload


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="4.0.0", lifespan=lifespan)


@app.middleware("http")
async def block_search_indexing(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    username_ok = secrets.compare_digest(credentials.username, settings.dashboard_username)
    password_ok = secrets.compare_digest(
        credentials.password, settings.dashboard_password.get_secret_value()
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증에 실패했습니다.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class ControlRequest(BaseModel):
    action: str
    profile: str | None = None
    enabled: bool | None = None


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots() -> str:
    return "User-agent: *\nDisallow: /\n"


@app.get("/health")
async def health() -> dict:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"status": "ok", "version": "4.0.0"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: str = Depends(authenticate)) -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@app.get("/decisions", response_class=HTMLResponse)
async def decisions_page(_: str = Depends(authenticate)) -> str:
    return DECISIONS_TEMPLATE.read_text(encoding="utf-8")


@app.get("/api/overview")
async def overview(
    _: str = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    config: Settings = Depends(get_settings),
) -> dict:
    state = await get_state(session)
    broker_error = None
    account = state.latest_account
    if not account_matches_mode(account, config.broker_mode):
        account = None
        if config.broker_api_enabled:
            broker = create_broker(config)
            try:
                snapshot = await broker.account_snapshot()
                account = account_payload(snapshot, config.broker_mode, "web")
                state.active_broker_mode = config.broker_mode
                state.latest_account = account
                add_portfolio_snapshot(session, snapshot, config.broker_mode)
                await session.commit()
            except Exception as exc:
                broker_error = friendly_error_message(str(exc))
            finally:
                await broker.close()
        else:
            broker_error = broker_wait_message(config)

    names = stock_name_map(account)
    current_strategy = replace_symbol_mentions(
        clean_empty_parentheses(koreanize_ai_text(state.current_strategy or "")),
        names,
    )
    market_view, market_links = split_source_links(
        replace_symbol_mentions(koreanize_ai_text(state.market_view or ""), names)
    )
    active_profile = get_profile(state.trading_profile or DEFAULT_PROFILE)
    active_limits = limits_for_profile(config, active_profile.key)

    return {
        "mode": config.broker_mode,
        "broker_api_enabled": config.broker_api_enabled,
        "execution_host": (
            "pc" if config.broker_mode == "toss" and not config.broker_api_enabled else "server"
        ),
        "live_trading_enabled": config.live_trading_enabled,
        "trading_profile": active_profile.key,
        "extended_hours_enabled": state.extended_hours_enabled,
        "day_market_enabled": state.day_market_enabled,
        "day_market_profile_allowed": active_profile.key in {"aggressive", "max_return"},
        "active_profile": {
            "key": active_profile.key,
            "label": active_profile.label,
            "short_label": active_profile.short_label,
            "description": active_profile.description,
        },
        "active_profile_limits": active_limits.pct_payload(),
        "profile_options": profile_options(),
        "state": {
            "trading_armed": state.trading_armed,
            "circuit_breaker": state.circuit_breaker,
            "breaker_reason": state.breaker_reason,
            "current_strategy": current_strategy,
            "market_view": market_view,
            "market_links": market_links,
            "latest_prices": state.latest_prices or {},
            "consecutive_failures": state.consecutive_failures,
            "last_cycle_at": state.last_cycle_at,
            "last_market_poll_at": state.last_market_poll_at,
            "market_open": state.market_open,
            "market_sessions": state.market_sessions,
        },
        "account": account,
        "broker_error": broker_error,
    }


@app.get("/api/decisions")
async def decisions(
    _: str = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    days: int = 7,
    limit: int = 200,
) -> list[dict]:
    days = max(1, min(days, 30))
    limit = max(1, min(limit, 500))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.scalars(
            select(DecisionLog)
            .where(DecisionLog.created_at >= cutoff)
            .order_by(DecisionLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    state = await get_state(session)
    latest_prices = state.latest_prices or {}
    holdings = holdings_by_symbol(state.latest_account)
    names = stock_name_map(state.latest_account)
    order_ids = [row.order_id for row in rows if row.order_id]
    trade_by_order = {}
    if order_ids:
        trades = (
            await session.scalars(select(TradeLog).where(TradeLog.order_id.in_(order_ids)))
        ).all()
        trade_by_order = {trade.order_id: trade for trade in trades}
    payload: list[dict] = []
    for row in rows:
        symbol = row.symbol.upper()
        clean_thesis, thesis_links = split_source_links(
            replace_symbol_mentions(koreanize_ai_text(row.thesis), names)
        )
        source_links = merge_source_links(thesis_links, evidence_source_links(row.evidence))
        payload.append(
            {
            "created_at": row.created_at,
            "market": row.market,
            "symbol": row.symbol,
            "name": names.get(symbol) or display_stock_name(row.symbol),
            "current_price": latest_prices.get(symbol),
            "last_price": holdings.get(symbol, {}).get("last_price"),
            "average_price": holdings.get(symbol, {}).get("average_price"),
            "holding_quantity": holdings.get(symbol, {}).get("quantity"),
            "market_value": holdings.get(symbol, {}).get("market_value"),
            "profit_loss": holdings.get(symbol, {}).get("profit_loss"),
            "profit_rate": holdings.get(symbol, {}).get("profit_rate"),
            "trade_price": (
                trade_by_order[row.order_id].price
                if row.order_id in trade_by_order
                else None
            ),
            "action": row.action,
            "confidence": row.confidence,
            "thesis": clean_thesis,
            "evidence": row.evidence,
            "source_links": source_links,
            "expected_return_pct": row.expected_return_pct,
            "risk_score": row.risk_score,
            "status": row.status,
            "rejection_reasons": row.rejection_reasons,
            "order_id": row.order_id,
            }
        )
    return payload


@app.get("/api/trades")
async def trades(
    _: str = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await session.scalars(select(TradeLog).order_by(TradeLog.created_at.desc()).limit(100))
    ).all()
    state = await get_state(session)
    holdings = holdings_by_symbol(state.latest_account)
    names = stock_name_map(state.latest_account)
    return [
        {
            "created_at": row.created_at,
            "source": row.source,
            "market": row.market,
            "symbol": row.symbol,
            "name": display_stock_name(
                row.symbol,
                {**(row.raw or {}), "name": names.get(row.symbol.upper()) or (row.raw or {}).get("name")},
            ),
            "current_price": holdings.get(row.symbol.upper(), {}).get("last_price")
            or (state.latest_prices or {}).get(row.symbol.upper()),
            "average_price": holdings.get(row.symbol.upper(), {}).get("average_price"),
            "holding_quantity": holdings.get(row.symbol.upper(), {}).get("quantity"),
            "side": row.side,
            "quantity": row.quantity,
            "price": row.price,
            "order_id": row.order_id,
            "status": row.status,
            "rationale": row.rationale,
        }
        for row in rows
    ]


@app.get("/api/audit")
async def audit_logs(
    _: str = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100))
    ).all()
    return [
        {
            "created_at": row.created_at,
            "level": row.level,
            "event_type": row.event_type,
            "message": row.message,
            "details": row.details,
        }
        for row in rows
    ]


@app.get("/api/performance")
async def performance(
    period: str = "day",
    _: str = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    config: Settings = Depends(get_settings),
) -> dict:
    period_days = {
        "day": 1,
        "week": 7,
        "month": 31,
    }
    if period not in period_days:
        raise HTTPException(status_code=400, detail="period는 day, week, month 중 하나여야 합니다.")

    since = datetime.now(timezone.utc) - timedelta(days=period_days[period])
    rows = (
        await session.scalars(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.broker_mode == config.broker_mode,
                PortfolioSnapshot.captured_at >= since,
            )
            .order_by(PortfolioSnapshot.captured_at.asc())
            .limit(2000)
        )
    ).all()
    if not rows:
        state = await get_state(session)
        account = state.latest_account or {}
        if account:
            return {
                "period": period,
                "points": [
                    {
                        "captured_at": account.get("captured_at"),
                        "profit_rate_pct": float(account.get("total_profit_rate") or 0) * 100,
                        "daily_return_pct": float(account.get("daily_return") or 0) * 100,
                        "equity_krw": account.get("equity_krw"),
                        "equity_usd": account.get("equity_usd"),
                        "cash_krw": account.get("cash_krw"),
                        "cash_usd": account.get("cash_usd"),
                    }
                ],
            }
    return {
        "period": period,
        "points": [
            {
                "captured_at": row.captured_at,
                "profit_rate_pct": row.total_profit_rate * 100,
                "daily_return_pct": row.daily_return * 100,
                "equity_krw": row.equity_krw,
                "equity_usd": row.equity_usd,
                "cash_krw": row.cash_krw,
                "cash_usd": row.cash_usd,
            }
            for row in rows
        ],
    }


@app.post("/api/control")
async def control_v2(
    request: ControlRequest,
    user: str = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    config: Settings = Depends(get_settings),
) -> dict:
    state = await get_state(session)
    action = request.action.lower()
    if action == "arm":
        if state.circuit_breaker:
            raise HTTPException(status_code=409, detail="오류 잠금을 먼저 해제해야 합니다.")
        if config.broker_mode == "toss" and not config.live_trading_enabled:
            raise HTTPException(
                status_code=409,
                detail="실거래를 켜려면 LIVE_TRADING_ENABLED=true가 필요합니다.",
            )
        state.trading_armed = True
        message = (
            "자동매매를 시작했습니다. 실제 주문은 AWS 고정 IP 주문봇이 처리합니다."
            if config.broker_mode == "toss" and not config.broker_api_enabled
            else "자동매매를 시작했습니다."
        )
    elif action == "disarm":
        state.trading_armed = False
        message = "자동매매를 중지했습니다."
    elif action == "reset_breaker":
        state.circuit_breaker = False
        state.breaker_reason = None
        state.consecutive_failures = 0
        message = "오류 잠금을 해제했습니다. 자동매매는 중지 상태입니다."
    elif action == "set_profile":
        if request.profile not in PROFILE_DEFINITIONS:
            raise HTTPException(status_code=400, detail="지원하지 않는 투자 성향입니다.")
        state.trading_profile = request.profile
        profile = get_profile(request.profile)
        message = f"투자 성향을 '{profile.label}'(으)로 변경했습니다. 다음 AI 분석부터 반영됩니다."
        if request.profile not in {"aggressive", "max_return"} and state.day_market_enabled:
            state.day_market_enabled = False
            message += " 미국 데이마켓 거래는 공격적/최대수익 전용이라 함께 꺼졌습니다."
    elif action == "set_extended_hours":
        if request.enabled is None:
            raise HTTPException(status_code=400, detail="enabled 값을 true 또는 false로 보내야 합니다.")
        state.extended_hours_enabled = bool(request.enabled)
        message = (
            "프리·애프터마켓 거래를 허용했습니다. 정규장 외 주문은 지정가만 사용합니다."
            if state.extended_hours_enabled
            else "프리·애프터마켓 거래를 끕니다. 정규장 주문만 허용합니다."
        )
    elif action == "set_day_market":
        if request.enabled is None:
            raise HTTPException(status_code=400, detail="enabled 값을 true 또는 false로 보내야 합니다.")
        if request.enabled and state.trading_profile not in {"aggressive", "max_return"}:
            raise HTTPException(
                status_code=409,
                detail="미국 데이마켓은 공격적 또는 최대수익 행동패턴에서만 켤 수 있습니다.",
            )
        state.day_market_enabled = bool(request.enabled)
        message = (
            "미국 데이마켓 거래를 허용했습니다. 프리·애프터 허용도 켜져 있고 공격적/최대수익 행동패턴일 때만 실제 작동합니다."
            if state.day_market_enabled
            else "미국 데이마켓 거래를 끕니다."
        )
    else:
        raise HTTPException(status_code=400, detail="지원하지 않는 제어 명령입니다.")
    await audit(
        session,
        "USER_CONTROL",
        message,
        details={
            "action": action,
            "user": user,
            "profile": request.profile,
            "extended_hours_enabled": state.extended_hours_enabled,
            "day_market_enabled": state.day_market_enabled,
        },
    )
    await session.commit()
    return {"ok": True, "message": message}
