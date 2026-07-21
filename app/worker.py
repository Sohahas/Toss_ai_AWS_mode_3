import asyncio
import logging
import signal

from app.config import get_settings
from app.db import init_db_with_retry
from app.engine import TradingEngine


async def main() -> None:
    settings = get_settings()
    if not settings.broker_api_enabled:
        raise RuntimeError(
            "BROKER_API_ENABLED=false에서는 worker를 실행할 수 없습니다. "
            "Render Web 전용 설정입니다. AWS/PC 주문봇은 BROKER_API_ENABLED=true로 실행하세요."
        )
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await init_db_with_retry(max_attempts=None)
    engine = TradingEngine(settings)
    await engine.record_heartbeat()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        loop_clock = asyncio.get_running_loop()
        next_analysis = 0.0
        while not stop.is_set():
            now = loop_clock.time()
            try:
                if now >= next_analysis:
                    await engine.run_cycle()
                    next_analysis = now + settings.analysis_interval_seconds
                else:
                    await engine.poll_market_data()
            except Exception:
                logging.getLogger(__name__).exception(
                    "워커 반복 작업 실패. 프로세스를 종료하지 않고 다음 주기에 자동 재시도합니다."
                )
            finally:
                await engine.record_heartbeat()
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.market_poll_interval_seconds
                )
            except TimeoutError:
                pass
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
