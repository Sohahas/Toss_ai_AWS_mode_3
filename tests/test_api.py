from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import Settings
from app.db import AuditLog, PaperCash, PaperHolding, SessionLocal, TradeLog
from app.main import account_matches_mode, app


async def clean_paper_state():
    async with SessionLocal() as session:
        await session.execute(delete(PaperHolding))
        await session.execute(delete(PaperCash))
        await session.execute(delete(TradeLog))
        await session.execute(
            delete(AuditLog).where(AuditLog.event_type == "PAPER_PORTFOLIO_REBUILT")
        )
        await session.commit()


def test_health_and_dashboard_authentication():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Disallow: /" in robots.text

        unauthorized = client.get("/")
        assert unauthorized.status_code == 401

        dashboard = client.get("/", auth=("admin", "change-me"))
        assert dashboard.status_code == 200
        assert "AI 주식 투자 비서" in dashboard.text


def test_paper_account_overview():
    import anyio

    anyio.run(clean_paper_state)
    with TestClient(app) as client:
        response = client.get("/api/overview", auth=("admin", "change-me"))
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "paper"
        assert body["account"]["cash_krw"] in {"10000000.0", "10000000"}
        assert body["account"]["cash_usd"] in {"10000.0", "10000"}
        assert body["trading_profile"] in {"balanced", "conservative", "aggressive", "hold", "max_return"}
        assert body["profile_options"]


def test_dashboard_profile_control_and_performance_api():
    with TestClient(app) as client:
        profile = client.post(
            "/api/control",
            auth=("admin", "change-me"),
            json={"action": "set_profile", "profile": "aggressive"},
        )
        assert profile.status_code == 200
        assert "투자 성향" in profile.json()["message"]

        overview = client.get("/api/overview", auth=("admin", "change-me"))
        assert overview.status_code == 200
        assert overview.json()["trading_profile"] == "aggressive"

        performance = client.get("/api/performance?period=week", auth=("admin", "change-me"))
        assert performance.status_code == 200
        assert performance.json()["period"] == "week"
        assert isinstance(performance.json()["points"], list)


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
