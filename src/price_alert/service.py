"""动态交易对池、ATR 预热、实时检测与重连编排。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from price_alert.config import INTERVAL_SECONDS, AppConfig
from price_alert.detector import AtrMoveDetector
from price_alert.gate import GateRestClient, GateTradeFeed
from price_alert.models import Candle, ContractTicker
from price_alert.notifier import AlertDispatcher, build_notifiers
from price_alert.universe import select_liquid_contracts

LOGGER = logging.getLogger(__name__)


def build_detector(config: AppConfig, cooldown_seconds: int | None = None) -> AtrMoveDetector:
    # 实时监控与 simulate 共用同一份组装逻辑，新增检测参数时只需改这里。
    indicator = config.indicator
    return AtrMoveDetector(
        atr_period=indicator.atr_period,
        candle_interval_seconds=INTERVAL_SECONDS[indicator.candle_interval],
        lookback_seconds=indicator.lookback_seconds,
        trigger_atr_multiple=indicator.trigger_atr_multiple,
        min_change_percent=indicator.min_change_percent,
        confirmation_seconds=indicator.confirmation_seconds,
        min_window_trades=indicator.min_window_trades,
        max_atr_age_seconds=indicator.max_atr_age_seconds,
        cooldown_seconds=config.alerts.cooldown_seconds if cooldown_seconds is None else cooldown_seconds,
    )


def _split_candles(
    candles: list[Candle],
    interval_seconds: int,
    now: datetime,
) -> tuple[list[Candle], Candle | None]:
    current_interval_start = int(now.timestamp()) // interval_seconds * interval_seconds
    closed = [candle for candle in candles if candle.timestamp.timestamp() < current_interval_start]
    current = next((candle for candle in candles if candle.timestamp.timestamp() == current_interval_start), None)
    return closed, current


async def _sync_universe(
    detector: AtrMoveDetector,
    rest: GateRestClient,
    selected: list[ContractTicker],
    config: AppConfig,
) -> None:
    selected_by_symbol = {ticker.symbol: ticker for ticker in selected}
    current = set(detector.symbols)
    target = set(selected_by_symbol)
    detector.remove_symbols(current - target)

    for symbol in current & target:
        detector.add_symbol(symbol, [], selected_by_symbol[symbol].volume_24h_quote)

    semaphore = asyncio.Semaphore(config.gate.warmup_concurrency)
    interval_seconds = INTERVAL_SECONDS[config.indicator.candle_interval]

    async def add_new_symbol(symbol: str) -> None:
        closed: list[Candle] = []
        live: Candle | None = None
        try:
            async with semaphore:
                fetched = await asyncio.to_thread(
                    rest.fetch_candles,
                    symbol,
                    config.indicator.candle_interval,
                    config.indicator.warmup_candles,
                )
            closed, live = _split_candles(fetched, interval_seconds, datetime.now(UTC))
        except Exception as exc:
            # 单个新品种预热失败时仍加入监控，它会在实时 K 线积累后自行就绪。
            LOGGER.warning("%s ATR 预热失败：%s", symbol, exc)
        detector.add_symbol(symbol, closed, selected_by_symbol[symbol].volume_24h_quote, live)

    await asyncio.gather(*(add_new_symbol(symbol) for symbol in sorted(target - current)))


async def _refresh_universe(
    detector: AtrMoveDetector,
    rest: GateRestClient,
    feed: GateTradeFeed,
    config: AppConfig,
) -> list[ContractTicker]:
    raw_tickers, raw_contracts = await asyncio.gather(
        asyncio.to_thread(rest.fetch_tickers),
        asyncio.to_thread(rest.fetch_contracts),
    )
    selected = select_liquid_contracts(
        raw_tickers,
        raw_contracts,
        config.gate.min_volume_24h_quote,
        retained_symbols=detector.symbols,
        exit_volume_ratio=config.gate.universe_exit_volume_ratio,
    )
    if not selected:
        raise RuntimeError("Gate.io 没有满足成交额条件的可用虚拟币合约")
    await _sync_universe(detector, rest, selected, config)
    # 先完成预热再订阅，新合约的第一笔实时成交到达时 ATR 已经就绪。
    await feed.set_symbols(detector.symbols)
    LOGGER.info(
        "交易对池已刷新：%d 个虚拟币合约，24h 计价成交额门槛 %.1fM USDT",
        len(selected),
        config.gate.min_volume_24h_quote / 1_000_000,
    )
    return selected


async def _initial_universe(
    detector: AtrMoveDetector,
    rest: GateRestClient,
    feed: GateTradeFeed,
    config: AppConfig,
) -> None:
    delay = config.gate.reconnect_initial_seconds
    while True:
        try:
            await _refresh_universe(detector, rest, feed, config)
            return
        except Exception as exc:
            LOGGER.error("交易对池初始化失败：%s；%.1f 秒后重试", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, config.gate.reconnect_max_seconds)


async def _universe_loop(
    detector: AtrMoveDetector,
    rest: GateRestClient,
    feed: GateTradeFeed,
    config: AppConfig,
) -> None:
    refresh_seconds = config.gate.universe_refresh_seconds
    delay = refresh_seconds
    while True:
        await asyncio.sleep(delay)
        try:
            await _refresh_universe(detector, rest, feed, config)
            delay = refresh_seconds
        except Exception as exc:
            # 刷新失败时沿用现有合约池继续监控，稍后重试即可，不影响实时连接。
            delay = min(60, refresh_seconds)
            LOGGER.error("交易对池刷新失败：%s；%d 秒后重试", exc, delay)


async def _stream_loop(
    detector: AtrMoveDetector,
    feed: GateTradeFeed,
    dispatcher: AlertDispatcher,
    config: AppConfig,
) -> None:
    reconnect_delay = config.gate.reconnect_initial_seconds
    last_status = time.monotonic()
    tick_count = 0

    while True:
        connected = False
        LOGGER.info("连接 Gate.io 实时成交：%d 个合约", len(feed.symbols))
        try:
            # aclosing 保证循环体抛错时也会立即关闭 WebSocket，而不是等垃圾回收。
            async with contextlib.aclosing(feed.stream()) as ticks:
                async for tick in ticks:
                    if not connected:
                        connected = True
                        reconnect_delay = config.gate.reconnect_initial_seconds
                        LOGGER.info("Gate.io 实时行情连接成功，已收到 %s 成交", tick.symbol)
                    tick_count += 1
                    for alert in detector.add_tick(tick):
                        dispatcher.publish(alert)
                    current_time = time.monotonic()
                    if current_time - last_status >= config.gate.status_interval_seconds:
                        rejected = len(feed.rejected_symbols)
                        LOGGER.info(
                            "监控正常：%d 个合约%s，累计 %s 条成交",
                            len(detector.symbols),
                            f"（{rejected} 个订阅被拒绝）" if rejected else "",
                            f"{tick_count:,}",
                        )
                        last_status = current_time
            raise ConnectionError("Gate.io 行情流意外结束")
        except Exception as exc:
            # 连接、握手、网络超时都会走到这里；合约池刷新已与连接解耦，任何异常都按故障退避重连。
            LOGGER.warning("Gate.io 行情连接异常：%s；%.1f 秒后重连", exc, reconnect_delay)
        detector.reset_realtime()
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, config.gate.reconnect_max_seconds)


async def run_monitor(config: AppConfig) -> None:
    detector = build_detector(config)
    rest = GateRestClient(
        config.gate.rest_url,
        config.gate.settle,
        config.gate.rest_timeout_seconds,
        config.gate.rest_retries,
    )
    feed = GateTradeFeed(
        config.gate.websocket_url,
        config.gate.subscription_chunk_size,
        config.gate.receive_timeout_seconds,
    )
    await _initial_universe(detector, rest, feed, config)
    async with AlertDispatcher(build_notifiers(config.alerts), config.alerts.queue_size) as dispatcher:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_universe_loop(detector, rest, feed, config))
            tasks.create_task(_stream_loop(detector, feed, dispatcher, config))
