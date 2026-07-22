from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import Settings
from app.db import AuditLog, PaperCash, PaperHolding, SessionLocal, TradeLog
from app.main import (
    account_matches_mode,
    aggregate_performance_rows,
    app,
    build_performance_buckets,
    display_stock_name,
    replace_symbol_mentions,
    stock_name_map,
)


def performance_row(captured_at: datetime, rate: float) -> SimpleNamespace:
    return SimpleNamespace(
        captured_at=captured_at,
        total_profit_rate=rate,
        daily_return=rate,
        equity_krw="1000000",
        equity_usd="1000",
        cash_krw="500000",
        cash_usd="500",
    )


def test_performance_time_axes_use_korean_time_and_requested_ranges():
    now = datetime(2026, 7, 19, 3, 30, tzinfo=timezone.utc)  # 한국시간 12:30

    day_buckets = build_performance_buckets("day", now=now)
    assert len(day_buckets) == 25
    assert day_buckets[0]["label"] == "00시"
    assert day_buckets[-1]["label"] == "24시"
    day_points = aggregate_performance_rows(
        [
            performance_row(datetime(2026, 7, 18, 15, 10, tzinfo=timezone.utc), 0.01),
            performance_row(datetime(2026, 7, 18, 15, 55, tzinfo=timezone.utc), 0.02),
            performance_row(datetime(2026, 7, 19, 1, 20, tzinfo=timezone.utc), 0.03),
        ],
        day_buckets,
    )
    assert day_points[0]["profit_rate_pct"] == 2.0
    assert day_points[10]["profit_rate_pct"] == 3.0
    assert day_points[24]["has_data"] is False

    week_buckets = build_performance_buckets("week", now=now)
    assert [bucket["label"] for bucket in week_buckets] == [
        "7/16",
        "7/17",
        "7/18",
        "7/19",
        "7/20",
        "7/21",
        "7/22",
    ]
    assert week_buckets[3]["is_current"] is True
    assert week_buckets[4]["accepts_data"] is False

    month_buckets = build_performance_buckets(
        "month",
        now=now,
        first_record_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert [bucket["label"] for bucket in month_buckets] == ["5월", "6월", "7월"]


async def clean_paper_state():
    async with SessionLocal() as session:
        await session.execute(delete(PaperHolding))
        await session.execute(delete(PaperCash))
        await session.execute(delete(TradeLog))
        await session.execute(
            delete(AuditLog).where(AuditLog.event_type == "PAPER_PORTFOLIO_REBUILT")
        )
        await session.commit()


def login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "change-me"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "aisa_session" in response.cookies


def test_health_and_dashboard_authentication():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Disallow: /" in robots.text

        unauthorized = client.get("/", follow_redirects=False)
        assert unauthorized.status_code == 303
        assert unauthorized.headers["location"] == "/login"

        login_page = client.get("/login")
        assert login_page.status_code == 200
        assert "아이디" in login_page.text

        login(client)
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "AI 주식 투자 비서" in dashboard.text
        assert "주문번호" in dashboard.text
        assert "관련 번호" in dashboard.text


def test_stock_names_replace_numeric_symbols_without_touching_parenthesized_code():
    names = {"005380": "현대차", "MSFT": "Microsoft"}
    assert display_stock_name("005380") == "현대차"
    assert display_stock_name("999999") == "종목명 미확인"
    assert stock_name_map(None, {"123456": "새 종목"})["123456"] == "새 종목"
    assert replace_symbol_mentions("KR 005380 FILLED", names) == "KR 현대차(005380) FILLED"
    assert replace_symbol_mentions("Microsoft(MSFT)", names) == "Microsoft(MSFT)"


def test_viewer_account_can_read_but_cannot_change_controls(monkeypatch):
    import importlib

    main_module = importlib.import_module("app.main")
    viewer_settings = Settings(
        _env_file=None,
        dashboard_username="owner",
        dashboard_password="owner-secret-password",
        dashboard_display_name="내 계정",
        viewer_username="brother",
        viewer_password="brother-secret-password",
        viewer_display_name="큰형",
    )
    monkeypatch.setattr(main_module, "settings", viewer_settings)

    with TestClient(app) as client:
        login_response = client.post(
            "/login",
            data={"username": "brother", "password": "brother-secret-password"},
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["session_user"] == {
            "username": "brother",
            "display_name": "큰형",
            "role": "viewer",
            "can_control": False,
        }

        blocked = client.post("/api/control", json={"action": "disarm"})
        assert blocked.status_code == 403
        assert "조회 전용" in blocked.json()["detail"]


def test_paper_account_overview():
    import anyio

    anyio.run(clean_paper_state)
    with TestClient(app) as client:
        login(client)
        response = client.get("/api/overview")
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "paper"
        assert body["account"]["cash_krw"] in {"10000000.0", "10000000"}
        assert body["account"]["cash_usd"] in {"10000.0", "10000"}
        assert body["trading_profile"] in {"balanced", "conservative", "aggressive", "hold", "max_return"}
        assert body["profile_options"]


def test_dashboard_profile_control_and_performance_api():
    with TestClient(app) as client:
        login(client)
        profile = client.post(
            "/api/control",
            json={"action": "set_profile", "profile": "aggressive"},
        )
        assert profile.status_code == 200
        assert "투자 성향" in profile.json()["message"]

        oco = client.post(
            "/api/control",
            json={
                "action": "set_oco",
                "enabled": True,
                "take_profit_pct": 9,
                "stop_loss_pct": 4,
            },
        )
        assert oco.status_code == 200

        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["trading_profile"] == "aggressive"
        assert overview.json()["oco_enabled"] is True

        performance = client.get("/api/performance?period=week")
        assert performance.status_code == 200
        assert performance.json()["period"] == "week"
        assert performance.json()["timezone"] == "Asia/Seoul"
        assert len(performance.json()["points"]) == 7
        assert all("label" in point for point in performance.json()["points"])

        orders = client.get("/api/orders")
        assert orders.status_code == 200
        assert "orders" in orders.json()
        assert "protections" in orders.json()


def test_account_mode_marker_rejects_stale_paper_snapshot_for_toss_mode():
    assert account_matches_mode({"cash_krw": "9580000.0", "_broker_mode": "paper"}, "toss") is False
    assert account_matches_mode({"cash_krw": "9580000.0"}, "toss") is False
    assert account_matches_mode({"cash_krw": "123", "_broker_mode": "toss"}, "toss") is True


def test_render_dashboard_can_use_toss_mode_without_toss_credentials():
    settings = Settings(
        _env_file=None,
        broker_mode="toss",
        broker_api_enabled=False,
        live_trading_enabled=True,
    )
    assert settings.broker_mode == "toss"
    assert settings.broker_api_enabled is False
    assert settings.live_trading_enabled is True


def test_render_external_postgres_url_is_normalized_for_asyncpg():
    settings = Settings(
        _env_file=None,
        database_url="postgres://user:pass@example.render.com/db?sslmode=require",
    )
    assert settings.database_url == (
        "postgresql+asyncpg://user:pass@example.render.com/db?ssl=true"
    )
