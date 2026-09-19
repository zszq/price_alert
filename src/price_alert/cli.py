"""Gate.io 价格异动提醒命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from price_alert.config import INTERVAL_SECONDS, AppConfig, load_config
from price_alert.gate import GateRestClient, RateLimiter
from price_alert.instance import AlreadyRunningError, ProcessLock
from price_alert.models import Candle, PriceTick
from price_alert.notifier import ConsoleNotifier
from price_alert.service import build_detector, run_monitor
from price_alert.universe import select_liquid_contracts

DEMO_ATR = 2.0


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


def _build_demo_candles(end: datetime, count: int, interval_seconds: int) -> list[Candle]:
    # 每根 K 线高低差恰好为 DEMO_ATR 且收盘不跳空，种子 ATR 因此确定等于 DEMO_ATR。
    half_range = DEMO_ATR / 2
    return [
        Candle(
            end - timedelta(seconds=interval_seconds * (count - index)),
            100.0,
            100.0 + half_range,
            100.0 - half_range,
            100.0,
        )
        for index in range(count)
    ]


async def simulate(config: AppConfig) -> int:
    indicator = config.indicator
    interval_seconds = INTERVAL_SECONDS[indicator.candle_interval]
    detector = build_detector(config, cooldown_seconds=0)
    notifier = ConsoleNotifier(beep=False, colors=config.alerts.console_colors)

    # 让异动恰好从 K 线边界开始：异动秒全部落在同一根未收盘 K 线里，不会被提前计入 ATR 而削弱倍数，
    # 同时之前的平稳 K 线按时收盘，ATR 始终新鲜，结果不随 lookback 等参数变化而失效。
    now = datetime.now(UTC)
    move_start = datetime.fromtimestamp(int(now.timestamp()) // interval_seconds * interval_seconds, tz=UTC)
    flat_start = move_start - timedelta(seconds=indicator.lookback_seconds + 1)
    history_end = datetime.fromtimestamp(
        int(flat_start.timestamp()) // interval_seconds * interval_seconds,
        tz=UTC,
    )
    detector.add_symbol(
        "BTC_USDT",
        _build_demo_candles(history_end, indicator.atr_period + 1, interval_seconds),
        1_000_000_000,
    )

    # 每秒成交笔数要足以让一个观察窗口满足 min_window_trades。
    trades_per_second = max(1, math.ceil(indicator.min_window_trades / indicator.lookback_seconds))

    def ticks_at(second: datetime, price: float, label: str) -> list[PriceTick]:
        return [
            PriceTick("BTC_USDT", price, 1.0, second + timedelta(milliseconds=index), f"{label}-{index}")
            for index in range(trades_per_second)
        ]

    for offset in range(indicator.lookback_seconds + 1):
        for tick in ticks_at(flat_start + timedelta(seconds=offset), 100.0, f"flat-{offset}"):
            detector.add_tick(tick)

    moved_price = 100.0 + max(DEMO_ATR * indicator.trigger_atr_multiple, indicator.min_change_percent) * 1.1
    alert_count = 0
    # 多送一秒用于结算最后一个确认桶，与真实行情的完整秒检测保持一致。
    for offset in range(indicator.confirmation_seconds + 1):
        for tick in ticks_at(move_start + timedelta(seconds=offset), moved_price, f"demo-{offset}"):
            for alert in detector.add_tick(tick):
                alert_count += 1
                await notifier.send(alert)
    return alert_count


def print_universe(config: AppConfig) -> None:
    rest = GateRestClient(
        config.gate.rest_url,
        config.gate.settle,
        config.gate.rest_timeout_seconds,
        config.gate.rest_retries,
        RateLimiter(config.gate.rest_rate_limit_per_second, config.gate.rest_rate_limit_burst),
    )
    selected = select_liquid_contracts(
        rest.fetch_tickers(),
        rest.fetch_contracts(),
        config.gate.min_volume_24h_quote,
    )
    print(f"当前符合条件：{len(selected)} 个 Gate.io 虚拟币 USDT 永续合约")
    for ticker in selected:
        print(f"{ticker.symbol:<20} {ticker.volume_24h_quote / 1_000_000:>12.2f}M USDT")


def _load_config_or_exit(path: str) -> AppConfig:
    try:
        return load_config(path)
    except FileNotFoundError as exc:
        raise SystemExit(f"配置文件不存在：{path}") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"配置文件 {path} 读取失败：{exc}") from exc
    except ValidationError as exc:
        # 逐项列出字段路径，比 pydantic 默认的英文堆栈更容易定位到 YAML 中的具体键。
        details = "\n".join(
            f"  - {'.'.join(str(part) for part in error['loc']) or '(根节点)'}：{error['msg']}"
            for error in exc.errors()
        )
        raise SystemExit(f"配置文件 {path} 校验失败：\n{details}") from exc


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    config = _load_config_or_exit(args.config)

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
        if asyncio.run(simulate(config)) == 0:
            raise SystemExit("模拟未产生提醒，请检查 indicator 配置是否互相矛盾")
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
