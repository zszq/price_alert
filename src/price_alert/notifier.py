"""控制台、JSONL 与通用 Webhook 提醒通道。"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Iterable
from datetime import timedelta, timezone
from pathlib import Path
from typing import Protocol

from colorama import Fore, Style, just_fix_windows_console

from price_alert.config import AlertConfig
from price_alert.models import PriceAlert

LOGGER = logging.getLogger(__name__)
BEIJING_TIME = timezone(timedelta(hours=8))


class Notifier(Protocol):
    async def send(self, alert: PriceAlert) -> None: ...


def format_alert(alert: PriceAlert) -> str:
    label = "暴涨" if alert.direction == "surge" else "暴跌"
    move_label = "上涨" if alert.direction == "surge" else "下跌"
    occurred_at = alert.timestamp.astimezone(BEIJING_TIME).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"[{label}提醒] {occurred_at} | {alert.symbol} | {alert.lookback_seconds}秒内价格{move_label} "
        f"{abs(alert.change_percent):.2f}% | {alert.reference_price:g} → {alert.price:g}"
        f" | 异动强度 {alert.move_atr:.2f} ATR"
    )


def colorize_alert(alert: PriceAlert, text: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    color = Fore.GREEN if alert.direction == "surge" else Fore.RED
    return f"{color}{text}{Style.RESET_ALL}"


class ConsoleNotifier:
    def __init__(self, beep: bool = True, colors: bool = True) -> None:
        self.beep = beep
        self.colors = colors
        if colors:
            # Windows 控制台实现差异较大，初始化兼容层可避免直接显示转义字符。
            just_fix_windows_console()

    async def send(self, alert: PriceAlert) -> None:
        text = colorize_alert(alert, format_alert(alert), self.colors)
        print(("\a" if self.beep else "") + text, flush=True)


class JsonlNotifier:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def send(self, alert: PriceAlert) -> None:
        await asyncio.to_thread(self._append, alert)

    def _append(self, alert: PriceAlert) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")


class WebhookNotifier:
    def __init__(self, url: str, timeout_seconds: float = 5.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def send(self, alert: PriceAlert) -> None:
        await asyncio.to_thread(self._post, alert)

    def _post(self, alert: PriceAlert) -> None:
        payload = alert.to_dict() | {"text": format_alert(alert)}
        request = urllib.request.Request(  # noqa: S310 - URL 由用户配置
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            if response.status >= 400:
                raise RuntimeError(f"Webhook 返回 HTTP {response.status}")


class NotifierHub:
    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self.notifiers = list(notifiers)

    async def send(self, alert: PriceAlert) -> None:
        results = await asyncio.gather(*(notifier.send(alert) for notifier in self.notifiers), return_exceptions=True)
        for notifier, result in zip(self.notifiers, results, strict=True):
            if isinstance(result, Exception):
                # 单个提醒出口失败不应中断全市场行情监控。
                LOGGER.error("提醒通道 %s 发送失败：%s", type(notifier).__name__, result)


def build_notifier(config: AlertConfig) -> NotifierHub:
    notifiers: list[Notifier] = []
    if config.console:
        notifiers.append(ConsoleNotifier(config.beep, config.console_colors))
    if config.jsonl_path is not None:
        notifiers.append(JsonlNotifier(config.jsonl_path))
    if config.webhook_url:
        notifiers.append(WebhookNotifier(config.webhook_url, config.webhook_timeout_seconds))
    return NotifierHub(notifiers)
