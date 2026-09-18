"""Gate.io USDT 永续公共 REST 与 WebSocket 行情适配器。"""

from __future__ import annotations

import asyncio
import http.client
import itertools
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import websockets

from price_alert.models import Candle, PriceTick

LOGGER = logging.getLogger(__name__)

# 只有限频和服务端临时故障值得重试；其余 4xx 多为参数或合约不存在，重试只会拖慢预热。
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class _ThrottledLogger:
    """交易所数据格式变化时每笔成交都会出错，限流可避免日志刷屏淹没真正的故障信息。"""

    def __init__(self, interval_seconds: float = 60.0) -> None:
        self.interval_seconds = interval_seconds
        self._last_logged = float("-inf")
        self._suppressed = 0

    def warning(self, message: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last_logged < self.interval_seconds:
            self._suppressed += 1
            return
        if self._suppressed:
            message = f"{message}（此前 {self._suppressed} 条同类日志已省略）"
        LOGGER.warning(message, *args)
        self._last_logged = now
        self._suppressed = 0


_INVALID_DATA_LOG = _ThrottledLogger()


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
            headers={"Accept": "application/json", "User-Agent": "price_alert/1.0"},
        )
        for attempt in range(self.retries):
            is_last_attempt = attempt == self.retries - 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # HTTPError 是 OSError 子类，必须先于下方分支处理，否则不可恢复的 4xx 也会被重试。
                if exc.code not in RETRYABLE_HTTP_STATUS or is_last_attempt:
                    raise
            except (OSError, http.client.HTTPException, json.JSONDecodeError):
                # 读响应体时的连接重置、截断和维护页等临时故障不会被包装成 URLError，需要单独纳入重试。
                if is_last_attempt:
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


def build_subscription(symbols: Sequence[str], event: str = "subscribe", request_id: int | None = None) -> str:
    request: dict[str, object] = {
        "time": int(time.time()),
        "channel": "futures.trades",
        "event": event,
        "payload": list(symbols),
    }
    if request_id is not None:
        # Gate 会在响应中原样回传 id，据此才能知道是哪一批订阅失败。
        request["id"] = request_id
    return json.dumps(request, separators=(",", ":"))


def _parse_trade(trade: Any) -> PriceTick | None:
    if not isinstance(trade, Mapping):
        raise TypeError("成交记录不是对象")
    # 官方说明 internal trade 可能显著偏离盘口且不会进入 K 线，不能用于异动判定。
    if trade.get("is_internal"):
        return None
    timestamp_value = trade.get("create_time_ms")
    if timestamp_value is None:
        timestamp_value = float(trade["create_time"]) * 1000
    timestamp_ms = float(timestamp_value)
    return PriceTick(
        symbol=str(trade["contract"]).upper(),
        price=float(trade["price"]),
        size=abs(float(trade["size"])),
        timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
        trade_id=str(trade.get("id")) if trade.get("id") is not None else None,
    )


def parse_trade_payload(payload: Mapping[str, Any]) -> list[PriceTick]:
    if payload.get("event") != "update" or payload.get("channel") != "futures.trades":
        return []

    result = payload.get("result")
    if not isinstance(result, list):
        _INVALID_DATA_LOG.warning("忽略 result 不是列表的 Gate 成交推送：%s", payload)
        return []

    ticks: list[PriceTick] = []
    for trade in result:
        # 单笔坏数据只丢弃这一笔；向上抛出会断开整条连接，让全部合约一起丢行情。
        try:
            tick = _parse_trade(trade)
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            _INVALID_DATA_LOG.warning("忽略无法解析的 Gate 成交（%s）：%s", exc, trade)
            continue
        if tick is not None:
            ticks.append(tick)
    return ticks


def parse_trade_message(message: str | bytes) -> list[PriceTick]:
    return parse_trade_payload(json.loads(message))


class GateTradeFeed:
    """长连接成交订阅；合约池变化通过增量订阅/退订生效，不必断线重连。"""

    def __init__(
        self,
        url: str,
        chunk_size: int = 100,
        receive_timeout_seconds: float = 30.0,
        connect: Callable[..., Any] = websockets.connect,
    ) -> None:
        self.url = url
        self.chunk_size = chunk_size
        self.receive_timeout_seconds = receive_timeout_seconds
        self.rejected_symbols: set[str] = set()
        self._connect = connect
        self._symbols: set[str] = set()
        self._websocket: Any = None
        # 订阅请求与集合变更必须串行，否则初始分批订阅与增量退订交错时，已移除的合约可能又被订阅回来。
        self._subscription_lock = asyncio.Lock()
        self._request_ids = itertools.count(1)
        self._pending: dict[int, tuple[str, list[str]]] = {}

    @property
    def symbols(self) -> list[str]:
        return sorted(self._symbols)

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        target = {symbol.upper() for symbol in symbols}
        async with self._subscription_lock:
            removed = self._symbols - target
            added = target - self._symbols
            self._symbols = target
            self.rejected_symbols &= target
            websocket = self._websocket
            if websocket is None:
                # 未连接时只记录目标集合，建立连接后会整体订阅。
                return
            try:
                await self._send_chunks(websocket, "unsubscribe", sorted(removed))
                await self._send_chunks(websocket, "subscribe", sorted(added))
            except Exception as exc:
                # 发送失败说明连接已断，stream 会重连并按最新集合整体订阅，这里无需重试。
                LOGGER.warning("增量更新订阅失败，将在重连后整体订阅：%s", exc)

    async def stream(self) -> AsyncIterator[PriceTick]:
        async with self._connect(
            self.url,
            ping_interval=20,
            # pong 与成交推送共用一条 TCP 流，链路拥塞时会被积压数据队头阻塞，RTT 可从
            # 200 毫秒涨到二十几秒。超时放宽到 30 秒，把断线判定让给 _stream_loop 里按
            # 成交时间戳做的滞后熔断（阈值更低、语义明确），keepalive 只兜底真正的死连接。
            ping_timeout=30,
            close_timeout=5,
            max_queue=20_000,
        ) as websocket:
            async with self._subscription_lock:
                self._pending.clear()
                self._websocket = websocket
                await self._send_chunks(websocket, "subscribe", sorted(self._symbols))
            try:
                while True:
                    try:
                        async with asyncio.timeout(self.receive_timeout_seconds):
                            message = await websocket.recv()
                    except TimeoutError as exc:
                        raise ConnectionError(f"连续 {self.receive_timeout_seconds:g} 秒未收到 Gate 行情") from exc

                    try:
                        payload = json.loads(message)
                    except ValueError:
                        _INVALID_DATA_LOG.warning("忽略无法解析的 Gate WebSocket 消息：%.200r", message)
                        continue
                    if not isinstance(payload, Mapping):
                        continue
                    if payload.get("event") in ("subscribe", "unsubscribe") or payload.get("error"):
                        await self._handle_response(websocket, payload)
                        continue
                    for tick in parse_trade_payload(payload):
                        yield tick
            finally:
                self._websocket = None

    async def _send_chunks(self, websocket: Any, event: str, symbols: list[str]) -> None:
        for start in range(0, len(symbols), self.chunk_size):
            chunk = symbols[start : start + self.chunk_size]
            request_id = next(self._request_ids)
            self._pending[request_id] = (event, chunk)
            await websocket.send(build_subscription(chunk, event, request_id))

    async def _handle_response(self, websocket: Any, payload: Mapping[str, Any]) -> None:
        request = self._pending.pop(payload["id"], None) if isinstance(payload.get("id"), int) else None
        error = payload.get("error")
        if not error:
            return
        if request is None:
            LOGGER.warning("Gate WebSocket 返回错误：%s", payload)
            return

        event, chunk = request
        if event != "subscribe":
            LOGGER.warning("Gate 退订 %s 失败：%s", ",".join(chunk), error)
            return
        async with self._subscription_lock:
            # 合约可能已在刷新中被移除，只处理仍需监控的部分。
            still_wanted = [symbol for symbol in chunk if symbol in self._symbols]
            if len(still_wanted) > 1:
                # 一个无效合约就可能让整批订阅失败，逐个重订阅以隔离问题合约，保住同批其他合约。
                LOGGER.warning("Gate 批量订阅 %d 个合约失败（%s），改为逐个订阅", len(still_wanted), error)
                for symbol in still_wanted:
                    await self._send_chunks(websocket, "subscribe", [symbol])
            elif still_wanted:
                self.rejected_symbols.update(still_wanted)
                LOGGER.error("Gate 拒绝订阅 %s，该合约不会被监控：%s", still_wanted[0], error)
