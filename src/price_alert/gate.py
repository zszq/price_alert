"""Gate.io USDT 永续公共 REST 与 WebSocket 行情适配器。"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import websockets

from price_alert.models import Candle, PriceTick


class GateRestClient:
    def __init__(
        self,
        base_url: str,
        settle: str = "usdt",
        timeout_seconds: float = 15.0,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.settle = settle
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def fetch_tickers(self) -> list[dict[str, Any]]:
        payload = self._get(f"/futures/{self.settle}/tickers")
        if not isinstance(payload, list):
            raise TypeError("Gate tickers 响应不是列表")
        return payload

    def fetch_contracts(self) -> list[dict[str, Any]]:
        payload = self._get(f"/futures/{self.settle}/contracts")
        if not isinstance(payload, list):
            raise TypeError("Gate contracts 响应不是列表")
        return payload

    def fetch_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        payload = self._get(
            f"/futures/{self.settle}/candlesticks",
            {"contract": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(payload, list):
            raise TypeError(f"Gate {symbol} K 线响应不是列表")
        candles = [parse_candle(item) for item in payload if isinstance(item, Mapping)]
        return sorted(candles, key=lambda item: item.timestamp)

    def _get(self, path: str, params: Mapping[str, object] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(  # noqa: S310 - 基础地址来自本地配置
            f"{self.base_url}{path}{query}",
            headers={"Accept": "application/json", "User-Agent": "price-alert/1.0"},
        )
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                    return json.loads(response.read().decode("utf-8"))
            except (TimeoutError, urllib.error.URLError):
                if attempt == self.retries - 1:
                    raise
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError("Gate REST 重试状态异常")


def parse_candle(payload: Mapping[str, Any]) -> Candle:
    return Candle(
        timestamp=datetime.fromtimestamp(float(payload["t"]), tz=UTC),
        open=float(payload["o"]),
        high=float(payload["h"]),
        low=float(payload["l"]),
        close=float(payload["c"]),
        quote_volume=float(payload.get("sum", 0.0)),
    )


def build_subscription(symbols: Sequence[str]) -> str:
    return json.dumps(
        {
            "time": int(time.time()),
            "channel": "futures.trades",
            "event": "subscribe",
            "payload": list(symbols),
        },
        separators=(",", ":"),
    )


def parse_trade_payload(payload: Mapping[str, Any]) -> list[PriceTick]:
    if payload.get("event") != "update" or payload.get("channel") != "futures.trades":
        return []

    ticks: list[PriceTick] = []
    for trade in payload.get("result", []):
        # 官方说明 internal trade 可能显著偏离盘口且不会进入 K 线，不能用于异动判定。
        if trade.get("is_internal"):
            continue
        timestamp_value = trade.get("create_time_ms")
        if timestamp_value is None:
            timestamp_value = float(trade["create_time"]) * 1000
        timestamp_ms = float(timestamp_value)
        ticks.append(
            PriceTick(
                symbol=str(trade["contract"]).upper(),
                price=float(trade["price"]),
                size=abs(float(trade["size"])),
                timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                trade_id=str(trade.get("id")) if trade.get("id") is not None else None,
            )
        )
    return ticks


def parse_trade_message(message: str | bytes) -> list[PriceTick]:
    return parse_trade_payload(json.loads(message))


class GateTradeFeed:
    def __init__(
        self,
        url: str,
        symbols: Sequence[str],
        chunk_size: int = 100,
        receive_timeout_seconds: float = 30.0,
    ) -> None:
        self.url = url
        self.symbols = list(symbols)
        self.chunk_size = chunk_size
        self.receive_timeout_seconds = receive_timeout_seconds

    async def stream(self) -> AsyncIterator[PriceTick]:
        async with websockets.connect(
            self.url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_queue=20_000,
        ) as websocket:
            for start in range(0, len(self.symbols), self.chunk_size):
                await websocket.send(build_subscription(self.symbols[start : start + self.chunk_size]))

            while True:
                try:
                    async with asyncio.timeout(self.receive_timeout_seconds):
                        message = await websocket.recv()
                except TimeoutError as exc:
                    raise ConnectionError(f"连续 {self.receive_timeout_seconds:g} 秒未收到 Gate 行情") from exc

                payload = json.loads(message)
                if payload.get("event") == "error":
                    raise RuntimeError(f"Gate WebSocket 错误：{payload}")
                for tick in parse_trade_payload(payload):
                    yield tick
