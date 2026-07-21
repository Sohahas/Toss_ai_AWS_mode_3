import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import koreanize_ai_text
from app.broker import KR_STOCK_NAMES, US_STOCK_NAMES, create_broker, friendly_error_message
from app.config import Settings, get_settings
from app.db import (
    AuditLog,
    DecisionLog,
    OrderIntent,
    PortfolioSnapshot,
    ProtectionOrder,
    TradeLog,
    add_portfolio_snapshot,
    audit,
    engine,
    get_session,
    get_state,
    init_db_with_retry,
)
from app.profiles import (
    DEFAULT_PROFILE,
    PROFILE_DEFINITIONS,
    get_profile,
    limits_for_profile,
    profile_options,
)

settings = get_settings()
TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"
DECISIONS_TEMPLATE = Path(__file__).parent / "templates" / "decisions.html"
LOGIN_TEMPLATE = Path(__file__).parent / "templates" / "login.html"
SESSION_COOKIE = "aisa_session"
LOGIN_FAILURES: dict[str, list[float]] = {}
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,80})\]\((https?://[^\s)]+)\)")
URL_RE = re.compile(r"https?://[^\s<>)\"']+")
KST = timezone(timedelta(hours=9))


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def build_performance_buckets(
    period: str,
    *,
    now: datetime | None = None,
    first_record_at: datetime | None = None,
) -> list[dict]:
    """한국시간 기준으로 그래프의 고정 시간축을 만든다."""
    now_kst = _aware_utc(now or datetime.now(timezone.utc)).astimezone(KST)
    buckets: list[dict] = []

    if period == "day":
        start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in range(25):
            bucket_start = start + timedelta(hours=hour)
            bucket_end = bucket_start + timedelta(hours=1)
            buckets.append(
                {
                    "key": f"{hour:02d}",
                    "label": f"{hour:02d}시",
                    "start": bucket_start,
                    "end": bucket_end,
                    "is_current": hour == now_kst.hour,
                    "accepts_data": hour < 24,
                }
            )
        return buckets

    if period == "week":
        today = now_kst.date()
        start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
        for offset in range(7):
            bucket_start = start + timedelta(days=offset)
            buckets.append(
                {
                    "key": bucket_start.date().isoformat(),
                    "label": f"{bucket_start.month}/{bucket_start.day}",
                    "start": bucket_start,
                    "end": bucket_start + timedelta(days=1),
                    "is_current": bucket_start.date() == today,
                    "accepts_data": bucket_start.date() <= today,
                }
            )
        return buckets

    if period == "month":
        first_kst = (
            _aware_utc(first_record_at).astimezone(KST)
            if first_record_at is not None
            else now_kst
        )
        cursor = first_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        current_month = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor <= current_month:
            same_year = cursor.year == now_kst.year
            buckets.append(
                {
                    "key": f"{cursor.year:04d}-{cursor.month:02d}",
                    "label": f"{cursor.month}월" if same_year else f"{str(cursor.year)[2:]}년 {cursor.month}월",
                    "start": cursor,
                    "end": _next_month(cursor),
                    "is_current": cursor == current_month,
                    "accepts_data": True,
                }
            )
            cursor = _next_month(cursor)
        return buckets

    raise ValueError("period는 day, week, month 중 하나여야 합니다.")


def aggregate_performance_rows(rows: list, buckets: list[dict]) -> list[dict]:
    """각 시간축 구간에서 가장 마지막으로 저장된 계좌 기록을 선택한다."""
    latest_by_key: dict[str, object] = {}
    for row in rows:
        captured_at = _aware_utc(row.captured_at).astimezone(KST)
        for bucket in buckets:
            if not bucket["accepts_data"]:
                continue
            if bucket["start"] <= captured_at < bucket["end"]:
                previous = latest_by_key.get(bucket["key"])
                if previous is None or _aware_utc(previous.captured_at) < _aware_utc(row.captured_at):
                    latest_by_key[bucket["key"]] = row
                break

    points: list[dict] = []
    for bucket in buckets:
        row = latest_by_key.get(bucket["key"])
        points.append(
            {
                "label": bucket["label"],
                "bucket_start": bucket["start"],
                "bucket_end": bucket["end"],
                "is_current": bucket["is_current"],
                "has_data": row is not None,
                "captured_at": row.captured_at if row is not None else None,
                "profit_rate_pct": float(row.total_profit_rate) * 100 if row is not None else None,
                "daily_return_pct": float(row.daily_return) * 100 if row is not None else None,
                "equity_krw": row.equity_krw if row is not None else None,
                "equity_usd": row.equity_usd if row is not None else None,
                "cash_krw": row.cash_krw if row is not None else None,
                "cash_usd": row.cash_usd if row is not None else None,
            }
        )
    return points


def apply_account_fallback(points: list[dict], account: dict | None) -> None:
    """DB 기록이 아직 없을 때 현재 구간에 최신 계좌값 하나만 표시한다."""
    if not account or any(point["has_data"] for point in points):
        return
    target = next((point for point in points if point["is_current"]), None)
    if target is None:
        return
    target.update(
        {
            "has_data": True,
            "captured_at": account.get("captured_at"),
            "profit_rate_pct": float(account.get("total_profit_rate") or 0) * 100,
            "daily_return_pct": float(account.get("daily_return") or 0) * 100,
            "equity_krw": account.get("equity_krw"),
            "equity_usd": account.get("equity_usd"),
            "cash_krw": account.get("cash_krw"),
            "cash_usd": account.get("cash_usd"),
        }
    )


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
    await init_db_with_retry(max_attempts=6)
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="4.1.3",
    lifespan=lifespan,
    docs_url=None if settings.environment == "production" else "/docs",
    redoc_url=None if settings.environment == "production" else "/redoc",
    openapi_url=None if settings.environment == "production" else "/openapi.json",
)


@app.middleware("http")
async def block_search_indexing(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _session_key() -> bytes:
    configured = settings.dashboard_session_secret
    source = (
        configured.get_secret_value()
        if configured is not None
        else f"{settings.dashboard_username}:{settings.dashboard_password.get_secret_value()}:aisa-session-v1"
    )
    return hashlib.sha256(source.encode("utf-8")).digest()


def _create_session_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + settings.dashboard_session_hours * 3600,
        "nonce": secrets.token_hex(8),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(_session_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _configured_users() -> list[dict[str, str]]:
    users = [
        {
            "username": settings.dashboard_username,
            "password": settings.dashboard_password.get_secret_value(),
            "display_name": settings.dashboard_display_name,
            "role": "admin",
        }
    ]
    if settings.viewer_username and settings.viewer_password is not None:
        users.append(
            {
                "username": settings.viewer_username,
                "password": settings.viewer_password.get_secret_value(),
                "display_name": settings.viewer_display_name,
                "role": "viewer",
            }
        )
    return users


def _user_by_username(username: str) -> dict[str, str] | None:
    for user in _configured_users():
        if secrets.compare_digest(username, user["username"]):
            return {key: value for key, value in user.items() if key != "password"}
    return None


def _session_user(request: Request) -> dict[str, str] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_session_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        username = str(payload.get("sub") or "")
        return _user_by_username(username)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def authenticate(request: Request) -> dict[str, str]:
    user = _session_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    return user


def require_admin(user: dict[str, str] = Depends(authenticate)) -> dict[str, str]:
    if user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="조회 전용 계정은 자동매매 설정을 변경할 수 없습니다.",
        )
    return user


class ControlRequest(BaseModel):
    action: str
    profile: str | None = None
    enabled: bool | None = None
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots() -> str:
    return "User-agent: *\nDisallow: /\n"


@app.get("/health")
async def health() -> dict:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"status": "ok", "version": "4.1.3"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@app.get("/health/worker")
async def worker_health(session: AsyncSession = Depends(get_session)) -> dict:
    state = await get_state(session)
    heartbeat = state.worker_heartbeat_at
    if heartbeat is None:
        raise HTTPException(status_code=503, detail="AWS 주문봇 심박 기록이 없습니다.")
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - heartbeat).total_seconds()))
    if age_seconds > settings.worker_stale_seconds:
        raise HTTPException(
            status_code=503,
            detail=f"AWS 주문봇 응답이 {age_seconds}초 동안 없습니다.",
        )
    return {"status": "ok", "age_seconds": age_seconds, "heartbeat_at": heartbeat}


def _login_page(error: str = "") -> HTMLResponse:
    html = LOGIN_TEMPLATE.read_text(encoding="utf-8").replace("{{ERROR}}", error)
    response = HTMLResponse(html)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    if _session_user(request):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@app.post("/login", include_in_schema=False)
async def login(request: Request):
    client = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [stamp for stamp in LOGIN_FAILURES.get(client, []) if now - stamp < 300]
    LOGIN_FAILURES[client] = recent
    if len(recent) >= 5:
        response = _login_page("로그인 시도가 너무 많습니다. 5분 후 다시 시도해 주세요.")
        response.status_code = 429
        return response
    body = (await request.body()).decode("utf-8", errors="replace")
    values = parse_qs(body)
    username = (values.get("username") or [""])[0]
    password = (values.get("password") or [""])[0]
    matched_user = None
    for candidate in _configured_users():
        username_ok = secrets.compare_digest(username, candidate["username"])
        password_ok = secrets.compare_digest(password, candidate["password"])
        if username_ok and password_ok:
            matched_user = candidate
    if matched_user is None:
        recent.append(now)
        LOGIN_FAILURES[client] = recent
        response = _login_page("아이디 또는 비밀번호가 올바르지 않습니다.")
        response.status_code = 401
        return response
    LOGIN_FAILURES.pop(client, None)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _create_session_token(matched_user["username"]),
        max_age=settings.dashboard_session_hours * 3600,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if _session_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    response = HTMLResponse(TEMPLATE.read_text(encoding="utf-8"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/decisions", response_class=HTMLResponse)
async def decisions_page(request: Request):
    if _session_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(DECISIONS_TEMPLATE.read_text(encoding="utf-8"))


@app.get("/api/overview")
async def overview(
    user: dict[str, str] = Depends(authenticate),
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
    now = datetime.now(timezone.utc)

    def age_seconds(value: datetime | None) -> int | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0, int((now - value).total_seconds()))

    heartbeat_age = age_seconds(state.worker_heartbeat_at)
    broker_age = age_seconds(state.last_market_poll_at)

    return {
        "session_user": {
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "can_control": user["role"] == "admin",
        },
        "mode": config.broker_mode,
        "broker_api_enabled": config.broker_api_enabled,
        "execution_host": "aws" if config.broker_mode == "toss" else "server",
        "live_trading_enabled": config.live_trading_enabled,
        "trading_profile": active_profile.key,
        "extended_hours_enabled": state.extended_hours_enabled,
        "day_market_enabled": state.day_market_enabled,
        "oco_enabled": state.oco_enabled,
        "oco_take_profit_pct": state.oco_take_profit_pct,
        "oco_stop_loss_pct": state.oco_stop_loss_pct,
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
            "worker_heartbeat_at": state.worker_heartbeat_at,
            "worker_started_at": state.worker_started_at,
            "worker_online": heartbeat_age is not None and heartbeat_age <= config.worker_stale_seconds,
            "worker_heartbeat_age_seconds": heartbeat_age,
            "broker_online": broker_age is not None and broker_age <= config.worker_stale_seconds,
            "broker_update_age_seconds": broker_age,
            "market_open": state.market_open,
            "market_sessions": state.market_sessions,
        },
        "account": account,
        "broker_error": broker_error,
    }


@app.get("/api/decisions")
async def decisions(
    _: dict[str, str] = Depends(authenticate),
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
            "name": row.name or names.get(symbol) or display_stock_name(row.symbol),
            "current_price": latest_prices.get(symbol) or row.reference_price,
            "decision_price": row.reference_price,
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
    _: dict[str, str] = Depends(authenticate),
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
            "execution": (row.raw or {}).get("order_detail", {}).get("raw", {}).get("execution")
            or (row.raw or {}).get("order_detail", {}).get("execution")
            or {},
        }
        for row in rows
    ]


@app.get("/api/orders")
async def order_statuses(
    _: dict[str, str] = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
) -> dict:
    intents = (
        await session.scalars(
            select(OrderIntent).order_by(OrderIntent.created_at.desc()).limit(50)
        )
    ).all()
    protections = (
        await session.scalars(
            select(ProtectionOrder).order_by(ProtectionOrder.created_at.desc()).limit(50)
        )
    ).all()
    return {
        "orders": [
            {
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "market": row.market,
                "symbol": row.symbol,
                "name": display_stock_name(row.symbol, row.raw),
                "side": row.side,
                "quantity": row.quantity,
                "order_amount": row.order_amount,
                "order_type": row.order_type,
                "price": row.price,
                "status": row.status,
                "order_id": row.order_id,
                "client_order_id": row.client_order_id,
                "last_error": row.last_error,
                "execution": (row.raw or {}).get("order_detail", {}).get("raw", {}).get("execution")
                or {},
            }
            for row in intents
        ],
        "protections": [
            {
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "market": row.market,
                "symbol": row.symbol,
                "name": display_stock_name(row.symbol),
                "quantity": row.quantity,
                "entry_price": row.entry_price,
                "take_profit_price": row.take_profit_price,
                "stop_trigger_price": row.stop_trigger_price,
                "stop_order_price": row.stop_order_price,
                "status": row.status,
                "conditional_order_id": row.conditional_order_id,
                "last_error": row.last_error,
            }
            for row in protections
        ],
    }


@app.get("/api/audit")
async def audit_logs(
    _: dict[str, str] = Depends(authenticate),
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
    _: dict[str, str] = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    config: Settings = Depends(get_settings),
) -> dict:
    if period not in {"day", "week", "month"}:
        raise HTTPException(status_code=400, detail="period는 day, week, month 중 하나여야 합니다.")

    now = datetime.now(timezone.utc)
    first_record_at = None
    if period == "month":
        first_record_at = await session.scalar(
            select(func.min(PortfolioSnapshot.captured_at)).where(
                PortfolioSnapshot.broker_mode == config.broker_mode
            )
        )
    buckets = build_performance_buckets(
        period, now=now, first_record_at=first_record_at
    )

    if period == "month":
        rows = []
        for bucket in buckets:
            row = await session.scalar(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.broker_mode == config.broker_mode,
                    PortfolioSnapshot.captured_at
                    >= bucket["start"].astimezone(timezone.utc),
                    PortfolioSnapshot.captured_at
                    < bucket["end"].astimezone(timezone.utc),
                )
                .order_by(PortfolioSnapshot.captured_at.desc())
                .limit(1)
            )
            if row is not None:
                rows.append(row)
    else:
        data_buckets = [bucket for bucket in buckets if bucket["accepts_data"]]
        range_start = data_buckets[0]["start"].astimezone(timezone.utc)
        range_end = data_buckets[-1]["end"].astimezone(timezone.utc)
        rows = (
            await session.scalars(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.broker_mode == config.broker_mode,
                    PortfolioSnapshot.captured_at >= range_start,
                    PortfolioSnapshot.captured_at < range_end,
                )
                .order_by(PortfolioSnapshot.captured_at.asc())
            )
        ).all()

    points = aggregate_performance_rows(list(rows), buckets)
    state = await get_state(session)
    apply_account_fallback(points, state.latest_account or {})
    return {
        "period": period,
        "timezone": "Asia/Seoul",
        "aggregation": "last",
        "range_start": buckets[0]["start"],
        "range_end": buckets[-1]["end"],
        "points": points,
    }


@app.post("/api/control")
async def control_v2(
    request: ControlRequest,
    user: dict[str, str] = Depends(require_admin),
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
    elif action == "set_oco":
        if request.enabled is None:
            raise HTTPException(status_code=400, detail="enabled 값을 true 또는 false로 보내야 합니다.")
        take_profit_pct = (
            request.take_profit_pct
            if request.take_profit_pct is not None
            else state.oco_take_profit_pct
        )
        stop_loss_pct = (
            request.stop_loss_pct
            if request.stop_loss_pct is not None
            else state.oco_stop_loss_pct
        )
        if not 1 <= take_profit_pct <= 50:
            raise HTTPException(status_code=400, detail="익절 기준은 1%~50%로 입력해 주세요.")
        if not 1 <= stop_loss_pct <= 20:
            raise HTTPException(status_code=400, detail="손절 기준은 1%~20%로 입력해 주세요.")
        state.oco_enabled = bool(request.enabled)
        state.oco_take_profit_pct = float(take_profit_pct)
        state.oco_stop_loss_pct = float(stop_loss_pct)
        message = (
            f"신규 매수 체결에 OCO 보호주문을 적용합니다. 익절 +{take_profit_pct:g}%, 손절 -{stop_loss_pct:g}%입니다."
            if state.oco_enabled
            else "신규 OCO 보호주문 생성을 껐습니다. 이미 등록된 보호주문은 안전을 위해 유지합니다."
        )
    else:
        raise HTTPException(status_code=400, detail="지원하지 않는 제어 명령입니다.")
    await audit(
        session,
        "USER_CONTROL",
        message,
        details={
            "action": action,
            "user": user["username"],
            "display_name": user["display_name"],
            "profile": request.profile,
            "extended_hours_enabled": state.extended_hours_enabled,
            "day_market_enabled": state.day_market_enabled,
            "oco_enabled": state.oco_enabled,
            "oco_take_profit_pct": state.oco_take_profit_pct,
            "oco_stop_loss_pct": state.oco_stop_loss_pct,
        },
    )
    await session.commit()
    return {"ok": True, "message": message}
