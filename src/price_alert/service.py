"""动态交易对池、ATR 预热、实时检测与重连编排。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from price_alert.config import AppConfig
from price_alert.detector import AtrMoveDetector
from price_alert.gate import GateRestClient, GateTradeFeed
from price_alert.models import Candle, ContractTicker
from price_alert.notifier import build_notifier
from price_alert.universe import select_liquid_contracts

LOGGER = logging.getLogger(__name__)

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900}


def _closed_candles(candles: list[Candle], interval_seconds: int, now: datetime) -> list[Candle]:
    current_interval_start = int(now.timestamp()) // interval_seconds * interval_seconds
    return [candle for candle in candles if candle.timestamp.timestamp() < current_interval_start]


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
        candles: list[Candle] = []
        try:
            async with semaphore:
                fetched = await asyncio.to_thread(
                    rest.fetch_candles,
                    symbol,
                    config.indicator.candle_interval,
                    config.indicator.warmup_candles,
                )
            candles = _closed_candles(fetched, interval_seconds, datetime.now(UTC))
        except Exception as exc:
            # 单个新品种预热失败时仍加入监控，它会在实时 K 线积累后自行就绪。
            LOGGER.warning("%s ATR 预热失败：%s", symbol, exc)
        detector.add_symbol(symbol, candles, selected_by_symbol[symbol].volume_24h_quote)

    await asyncio.gather(*(add_new_symbol(symbol) for symbol in sorted(target - current)))


async def _refresh_universe(
    detector: AtrMoveDetector,
    rest: GateRestClient,
    config: AppConfig,
) -> list[ContractTicker]:
    raw_tickers, raw_contracts = await asyncio.gather(
        asyncio.to_thread(rest.fetch_tickers),
        asyncio.to_thread(rest.fetch_contracts),
    )
    selected = select_liquid_contracts(raw_tickers, raw_contracts, config.gate.min_volume_24h_quote)
    if not selected:
        raise RuntimeError("Gate.io 没有满足成交额条件的可用虚拟币合约")
    await _sync_universe(detector, rest, selected, config)
    LOGGER.info(
        "交易对池已刷新：%d 个虚拟币合约，24h 计价成交额门槛 %.1fM USDT",
        len(selected),
        config.gate.min_volume_24h_quote / 1_000_000,
    )
    return selected


async def run_monitor(config: AppConfig) -> None:
    interval_seconds = INTERVAL_SECONDS[config.indicator.candle_interval]
    detector = AtrMoveDetector(
        atr_period=config.indicator.atr_period,
        candle_interval_seconds=interval_seconds,
        lookback_seconds=config.indicator.lookback_seconds,
        trigger_atr_multiple=config.indicator.trigger_atr_multiple,
        min_change_percent=config.indicator.min_change_percent,
        confirmation_seconds=config.indicator.confirmation_seconds,
        min_window_trades=config.indicator.min_window_trades,
        max_atr_age_seconds=config.indicator.max_atr_age_seconds,
        cooldown_seconds=config.alerts.cooldown_seconds,
    )
    rest = GateRestClient(
        config.gate.rest_url,
        config.gate.settle,
        config.gate.rest_timeout_seconds,
        config.gate.rest_retries,
    )
    notifier = build_notifier(config.alerts)
    reconnect_delay = config.gate.reconnect_initial_seconds
    next_universe_refresh = 0.0
    last_status = time.monotonic()
    tick_count = 0

    while True:
        now = time.monotonic()
        if now >= next_universe_refresh:
            try:
                await _refresh_universe(detector, rest, config)
                next_universe_refresh = time.monotonic() + config.gate.universe_refresh_seconds
            except Exception as exc:
                LOGGER.error("交易对池刷新失败：%s", exc)
                if not detector.symbols:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, config.gate.reconnect_max_seconds)
                    continue
                next_universe_refresh = time.monotonic() + 60

        feed = GateTradeFeed(
            config.gate.websocket_url,
            detector.symbols,
            config.gate.subscription_chunk_size,
            config.gate.receive_timeout_seconds,
        )
        session_seconds = max(1.0, next_universe_refresh - time.monotonic())
        connected = False
        try:
            LOGGER.info("连接 Gate.io 实时成交：%d 个合约", len(detector.symbols))
            async with asyncio.timeout(session_seconds):
                async for tick in feed.stream():
                    if not connected:
                        connected = True
                        reconnect_delay = config.gate.reconnect_initial_seconds
                        LOGGER.info("Gate.io 实时行情连接成功，已收到 %s 成交", tick.symbol)
                    tick_count += 1
                    for alert in detector.add_tick(tick):
                        await notifier.send(alert)
                    current_time = time.monotonic()
                    if current_time - last_status >= config.gate.status_interval_seconds:
                        LOGGER.info(
                            "监控正常：%d 个合约，累计 %s 条成交",
                            len(detector.symbols),
                            f"{tick_count:,}",
                        )
                        last_status = current_time
        except TimeoutError:
            LOGGER.info("到达交易对池刷新周期，重新筛选合约")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Gate.io 行情连接异常：%s；%.1f 秒后重连", exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, config.gate.reconnect_max_seconds)
