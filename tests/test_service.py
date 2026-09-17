import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from price_alert import service
from price_alert.config import AppConfig
from price_alert.gate import GateTradeFeed
from price_alert.models import Candle, PriceTick

BASE = datetime(2026, 1, 1, tzinfo=UTC)


class StopLoop(BaseException):
    """跳出无限循环专用；继承 BaseException 以免被服务里的 except Exception 吞掉。"""


def record_sleeps(monkeypatch, limit: int) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= limit:
            raise StopLoop

    monkeypatch.setattr(service.asyncio, "sleep", fake_sleep)
    return delays


def config() -> AppConfig:
    return AppConfig.model_validate(
        {"gate": {"reconnect_initial_seconds": 1, "reconnect_max_seconds": 4, "universe_refresh_seconds": 600}}
    )


class FakeDetector:
    def __init__(self) -> None:
        self.resets = 0

    @property
    def symbols(self) -> list[str]:
        return ["BTC_USDT"]

    def add_tick(self, tick: PriceTick) -> list[str]:
        return [f"alert-{tick.trade_id}"]

    def reset_realtime(self) -> None:
        self.resets += 1


class ScriptedFeed:
    """每次 stream() 依次执行一个脚本：先产出若干成交，再抛出指定异常。"""

    def __init__(self, scripts) -> None:
        self.scripts = list(scripts)
        self.rejected_symbols: set[str] = set()
        self.symbols = ["BTC_USDT"]

    async def stream(self):
        ticks, error = self.scripts.pop(0)
        for trade_id in ticks:
            yield PriceTick("BTC_USDT", 100.0, 1.0, BASE, trade_id)
        raise error


class FakeDispatcher:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, alert) -> None:
        self.published.append(alert)


def test_network_timeouts_use_backoff_and_reset_realtime_state(monkeypatch, caplog):
    delays = record_sleeps(monkeypatch, limit=3)
    detector, dispatcher = FakeDetector(), FakeDispatcher()
    feed = ScriptedFeed(
        [
            # 握手或 TCP 连接超时抛出的就是 TimeoutError，必须按故障退避而不是当作刷新周期。
            ((), TimeoutError("timed out during opening handshake")),
            (("1", "2"), ConnectionError("closed")),
            ((), TimeoutError("connect timeout")),
        ]
    )

    with pytest.raises(StopLoop):
        asyncio.run(service._stream_loop(detector, feed, dispatcher, config()))

    # 第二次连接收到成交后退避重置为初始值。
    assert delays == [1, 1, 2]
    assert detector.resets == 3
    assert dispatcher.published == ["alert-1", "alert-2"]
    assert "timed out during opening handshake" in caplog.text
    assert "刷新周期" not in caplog.text


class FakeRest:
    def __init__(self, tickers, contracts, fail_times: int = 0) -> None:
        self.tickers = tickers
        self.contracts = contracts
        self.fail_times = fail_times
        self.candle_requests: list[str] = []

    def fetch_tickers(self):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("rest down")
        return self.tickers

    def fetch_contracts(self):
        return self.contracts

    def fetch_candles(self, symbol, interval, limit):
        self.candle_requests.append(symbol)
        return []


def ticker(symbol: str, volume: float) -> dict:
    return {"contract": symbol, "last": "100", "volume_24h_quote": str(volume)}


def contract(symbol: str) -> dict:
    return {"name": symbol, "contract_type": "", "status": "trading"}


def test_refresh_warms_new_symbols_keeps_hysteresis_and_updates_feed_without_reconnect():
    async def scenario():
        app_config = config()
        detector = service.build_detector(app_config)
        feed = GateTradeFeed("wss://example.test")
        first = FakeRest(
            [ticker("BTC_USDT", 20e6), ticker("ETH_USDT", 20e6)],
            [contract("BTC_USDT"), contract("ETH_USDT")],
        )
        await service._refresh_universe(detector, first, feed, app_config)

        # ETH 成交额回落到门槛与退出门槛之间应保留，BTC 跌破退出门槛被移除，SOL 新入池需要预热。
        second = FakeRest(
            [ticker("BTC_USDT", 5e6), ticker("ETH_USDT", 9e6), ticker("SOL_USDT", 20e6)],
            [contract("BTC_USDT"), contract("ETH_USDT"), contract("SOL_USDT")],
        )
        await service._refresh_universe(detector, second, feed, app_config)
        return detector, feed, first, second

    detector, feed, first, second = asyncio.run(scenario())

    assert first.candle_requests == ["BTC_USDT", "ETH_USDT"]
    assert second.candle_requests == ["SOL_USDT"]
    assert detector.symbols == ["ETH_USDT", "SOL_USDT"]
    assert feed.symbols == ["ETH_USDT", "SOL_USDT"]


def test_split_candles_separates_unclosed_candle():
    now = BASE + timedelta(minutes=5, seconds=20)
    candles = [Candle(BASE + timedelta(minutes=minute), 100, 101, 99, 100) for minute in range(3, 6)]

    closed, live = service._split_candles(candles, 60, now)

    assert [candle.timestamp for candle in closed] == [BASE + timedelta(minutes=3), BASE + timedelta(minutes=4)]
    assert live is not None and live.timestamp == BASE + timedelta(minutes=5)


def test_universe_loop_keeps_running_after_refresh_failure(monkeypatch):
    delays = record_sleeps(monkeypatch, limit=3)
    app_config = config()
    detector = service.build_detector(app_config)
    rest = FakeRest([ticker("BTC_USDT", 20e6)], [contract("BTC_USDT")], fail_times=1)
    feed = GateTradeFeed("wss://example.test")

    with pytest.raises(StopLoop):
        asyncio.run(service._universe_loop(detector, rest, feed, app_config))

    assert delays == [600, 60, 600]
    assert detector.symbols == ["BTC_USDT"]


def test_initial_universe_retries_with_backoff(monkeypatch):
    delays = record_sleeps(monkeypatch, limit=10)
    app_config = config()
    detector = service.build_detector(app_config)
    rest = FakeRest([ticker("BTC_USDT", 20e6)], [contract("BTC_USDT")], fail_times=4)
    feed = GateTradeFeed("wss://example.test")

    asyncio.run(service._initial_universe(detector, rest, feed, app_config))

    assert delays == [1, 2, 4, 4]
    assert feed.symbols == ["BTC_USDT"]
