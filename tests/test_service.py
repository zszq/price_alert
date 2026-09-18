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
        self.gaps = 0

    @property
    def symbols(self) -> list[str]:
        return ["BTC_USDT"]

    @property
    def stale_symbols(self) -> list[str]:
        return ["BTC_USDT"] if self.gaps else []

    def add_tick(self, tick: PriceTick) -> list[str]:
        return [f"alert-{tick.trade_id}"]

    def mark_stream_gap(self) -> None:
        self.gaps += 1


class ScriptedFeed:
    """每次 stream() 依次执行一个脚本：先产出若干成交，再抛出指定异常。

    成交时间戳取当前时间，避免被 _stream_loop 的滞后熔断当成积压数据断开；
    需要构造滞后成交的用例传 lag_seconds。
    """

    def __init__(self, scripts, lag_seconds: float = 0.0) -> None:
        self.scripts = list(scripts)
        self.lag_seconds = lag_seconds
        self.rejected_symbols: set[str] = set()
        self.symbols = ["BTC_USDT"]

    async def stream(self):
        ticks, error = self.scripts.pop(0)
        timestamp = datetime.now(UTC) - timedelta(seconds=self.lag_seconds)
        for trade_id in ticks:
            yield PriceTick("BTC_USDT", 100.0, 1.0, timestamp, trade_id)
        # 让出一次事件循环，使连接期间启动的后台回补任务真正开始运行。
        wakeup = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(wakeup.set_result, None)
        await wakeup
        raise error


class FakeDispatcher:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, alert) -> None:
        self.published.append(alert)


def test_network_timeouts_use_backoff_mark_gap_and_resync_only_while_connected(monkeypatch, caplog):
    delays = record_sleeps(monkeypatch, limit=3)
    resync_started: list[int] = []
    resync_cancelled: list[int] = []

    async def fake_resync(detector, rest, config):
        resync_started.append(len(resync_started))
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            resync_cancelled.append(len(resync_cancelled))
            raise

    monkeypatch.setattr(service, "_resync_stale_symbols", fake_resync)
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
        asyncio.run(service._stream_loop(detector, feed, object(), dispatcher, config()))

    # 第二次连接收到成交后退避重置为初始值。
    assert delays == [1, 1, 2]
    # 三次断线各标记一次，第二次连接恢复时再标记一次，覆盖断线期间新入池的合约。
    assert detector.gaps == 4
    # 只有第二次连接真正收到成交时才开始回补，且断线时回补被取消。
    assert resync_started == [0]
    assert resync_cancelled == [0]
    assert dispatcher.published == ["alert-1", "alert-2"]
    assert "timed out during opening handshake" in caplog.text
    assert "刷新周期" not in caplog.text


def test_first_stale_tick_breaks_before_publishing(monkeypatch, caplog):
    """连接后的第一笔成交就滞后时必须立即熔断：不能留出放行积压成交的时间窗口。

    刻意不替换单调时钟——按时间间隔抽查的实现会让首笔落进盲区，用跳变的假时钟
    反而会掩盖它。
    """
    delays = record_sleeps(monkeypatch, limit=1)
    detector, dispatcher = FakeDetector(), FakeDispatcher()
    feed = ScriptedFeed([(("1", "2"), ConnectionError("unused"))], lag_seconds=30)

    with pytest.raises(StopLoop):
        asyncio.run(service._stream_loop(detector, feed, object(), dispatcher, config()))

    assert "行情数据滞后" in caplog.text
    assert "30.0 秒" in caplog.text
    # 校验在判定之前，且首笔即熔断，超限的成交一笔都不会产生已经过期的提醒。
    assert dispatcher.published == []
    # 熔断走正常的断线路径：清空秒级窗口并按初始退避重连。
    assert detector.gaps == 1
    assert delays == [1]


def test_backlog_burst_within_one_second_is_not_let_through(monkeypatch):
    """同一秒内涌入的大批积压成交必须全部拦下，不能只抽查其中一笔。"""
    record_sleeps(monkeypatch, limit=1)
    detector, dispatcher = FakeDetector(), FakeDispatcher()
    # 一次性给出 50 笔滞后成交，模拟积压排空时的突发。
    feed = ScriptedFeed([(tuple(str(i) for i in range(50)), ConnectionError("unused"))], lag_seconds=30)

    with pytest.raises(StopLoop):
        asyncio.run(service._stream_loop(detector, feed, object(), dispatcher, config()))

    assert dispatcher.published == []


def test_fresh_market_data_does_not_trigger_lag_breaker(monkeypatch):
    """数据新鲜时不得误断连接。"""
    record_sleeps(monkeypatch, limit=1)
    detector, dispatcher = FakeDetector(), FakeDispatcher()
    feed = ScriptedFeed([(("1", "2", "3"), ConnectionError("down"))], lag_seconds=0)

    with pytest.raises(StopLoop):
        asyncio.run(service._stream_loop(detector, feed, object(), dispatcher, config()))

    assert dispatcher.published == ["alert-1", "alert-2", "alert-3"]


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

    # 新合约经 to_thread 并发预热，请求先后取决于线程调度，只校验请求集合。
    assert sorted(first.candle_requests) == ["BTC_USDT", "ETH_USDT"]
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


class CandleRest:
    """按合约返回 K 线，可让指定合约前几次请求失败。"""

    def __init__(self, candles, failures: dict[str, int] | None = None) -> None:
        self.candles = candles
        self.failures = dict(failures or {})
        self.requests: list[str] = []

    def fetch_candles(self, symbol, interval, limit):
        self.requests.append(symbol)
        if self.failures.get(symbol):
            self.failures[symbol] -= 1
            raise ConnectionError("candles down")
        return self.candles


def test_resync_rebuilds_stale_symbols_and_retries_failures(monkeypatch):
    delays = record_sleeps(monkeypatch, limit=10)
    app_config = config()
    detector = service.build_detector(app_config)
    now = datetime.now(UTC)
    candles = [
        Candle(now - timedelta(minutes=offset), 100, 101, 99, 100)
        for offset in range(app_config.indicator.warmup_candles, -1, -1)
    ]
    for symbol in ("BTC_USDT", "ETH_USDT"):
        detector.add_symbol(symbol, [], 1e9)
    detector.mark_stream_gap()
    rest = CandleRest(candles, failures={"ETH_USDT": 1})

    asyncio.run(service._resync_stale_symbols(detector, rest, app_config))

    assert detector.stale_symbols == []
    # 第一轮 ETH 失败，只按退避重试仍然失效的合约。
    assert sorted(rest.requests) == ["BTC_USDT", "ETH_USDT", "ETH_USDT"]
    assert delays == [1]


def test_symbols_added_during_disconnect_are_resynced_after_reconnect(monkeypatch):
    record_sleeps(monkeypatch, limit=2)
    app_config = config()
    detector = service.build_detector(app_config)
    detector.add_symbol("BTC_USDT", [], 1e9)
    resynced_batches: list[list[str]] = []

    async def fake_resync(detector, rest, config):
        resynced_batches.append(detector.stale_symbols)

    class AddsSymbolWhileDisconnected(ScriptedFeed):
        async def stream(self):
            if len(self.scripts) == 1:
                # 模拟断线退避期间合约池刷新加入了新合约（预热早于重连）。
                detector.add_symbol("NEW_USDT", [], 1e9)
            async for tick in super().stream():
                yield tick

    monkeypatch.setattr(service, "_resync_stale_symbols", fake_resync)
    feed = AddsSymbolWhileDisconnected([((), ConnectionError("down")), (("1",), ConnectionError("down again"))])

    with pytest.raises(StopLoop):
        asyncio.run(service._stream_loop(detector, feed, object(), FakeDispatcher(), app_config))

    assert resynced_batches == [["BTC_USDT", "NEW_USDT"]]
