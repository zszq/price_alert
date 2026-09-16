"""Gate.io 价格异动提醒命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from price_alert.config import AppConfig, load_config
from price_alert.detector import AtrMoveDetector
from price_alert.gate import GateRestClient
from price_alert.instance import AlreadyRunningError, ProcessLock
from price_alert.models import Candle, PriceTick
from price_alert.notifier import ConsoleNotifier
from price_alert.service import INTERVAL_SECONDS, run_monitor
from price_alert.universe import select_liquid_contracts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gate.io 合约价格异动检测与提醒")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "启动 Gate.io 全市场实时监控"),
        ("universe", "查看当前满足成交额条件的合约"),
        ("check-config", "校验配置文件"),
        ("simulate", "使用合成行情验证 ATR 异动提醒"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", default="config/default.yaml")
    return parser


def _build_demo_candles(start: datetime, count: int) -> list[Candle]:
    return [
        Candle(start + timedelta(minutes=index), 100.0, 101.0, 99.0, 100.0)
        for index in range(count)
    ]


async def simulate(config: AppConfig) -> None:
    indicator = config.indicator
    interval_seconds = INTERVAL_SECONDS[indicator.candle_interval]
    detector = AtrMoveDetector(
        atr_period=indicator.atr_period,
        candle_interval_seconds=interval_seconds,
        lookback_seconds=indicator.lookback_seconds,
        trigger_atr_multiple=indicator.trigger_atr_multiple,
        min_change_percent=indicator.min_change_percent,
        confirmation_seconds=indicator.confirmation_seconds,
        min_window_trades=indicator.min_window_trades,
        max_atr_age_seconds=indicator.max_atr_age_seconds,
        cooldown_seconds=0,
    )
    notifier = ConsoleNotifier(beep=False)
    now = datetime.now(UTC).replace(microsecond=0)
    history_start = now - timedelta(minutes=indicator.atr_period + 2)
    detector.add_symbol("BTC_USDT", _build_demo_candles(history_start, indicator.atr_period + 1), 1_000_000_000)

    for second in range(indicator.lookback_seconds + 1):
        detector.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, now + timedelta(seconds=second), str(second)))
    atr = 2.0
    atr_distance = atr * indicator.trigger_atr_multiple
    percent_distance = 100.0 * indicator.min_change_percent / 100.0
    moved_price = 100.0 + max(atr_distance, percent_distance) * 1.1
    # 多送一秒用于结算最后一个确认桶，与真实行情的完整秒检测保持一致。
    for offset in range(indicator.confirmation_seconds + 1):
        alert_tick = PriceTick(
            "BTC_USDT",
            moved_price,
            1.0,
            now + timedelta(seconds=indicator.lookback_seconds + 1 + offset),
            f"demo-{offset}",
        )
        for alert in detector.add_tick(alert_tick):
            await notifier.send(alert)


def print_universe(config: AppConfig) -> None:
    rest = GateRestClient(
        config.gate.rest_url,
        config.gate.settle,
        config.gate.rest_timeout_seconds,
        config.gate.rest_retries,
    )
    selected = select_liquid_contracts(
        rest.fetch_tickers(),
        rest.fetch_contracts(),
        config.gate.min_volume_24h_quote,
    )
    print(f"当前符合条件：{len(selected)} 个 Gate.io 虚拟币 USDT 永续合约")
    for ticker in selected:
        print(f"{ticker.symbol:<20} {ticker.volume_24h_quote / 1_000_000:>12.2f}M USDT")


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    config = load_config(args.config)

    if args.command == "check-config":
        print(
            f"配置有效：Gate.io USDT 永续，成交额门槛 {config.gate.min_volume_24h_quote / 1_000_000:.1f}M，"
            f"触发阈值 {config.indicator.trigger_atr_multiple:g} ATR，"
            f"最低涨跌 {config.indicator.min_change_percent:g}%，"
            f"连续 {config.indicator.confirmation_seconds} 秒确认"
        )
        return
    if args.command == "universe":
        print_universe(config)
        return
    if args.command == "simulate":
        asyncio.run(simulate(config))
        return
    try:
        with ProcessLock(Path("data/price-alert.lock")):
            asyncio.run(run_monitor(config))
    except AlreadyRunningError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        print("\n监控已停止")


if __name__ == "__main__":
    main()
