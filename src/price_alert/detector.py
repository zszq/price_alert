"""使用 ATR 标准化短时价格位移，识别动态异动。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from price_alert.indicators import WilderAtr
from price_alert.models import Candle, PriceAlert, PriceTick


@dataclass(slots=True)
class _SecondBucket:
    timestamp: datetime
    weighted_price_sum: float = 0.0
    total_size: float = 0.0
    price_sum: float = 0.0
    trade_count: int = 0
    last_price: float | None = None

    @classmethod
    def carried(cls, timestamp: datetime, price: float) -> _SecondBucket:
        return cls(timestamp, last_price=price)

    @property
    def has_trades(self) -> bool:
        return self.trade_count > 0

    def add(self, price: float, size: float) -> None:
        self.price_sum += price
        self.trade_count += 1
        self.last_price = price
        if size > 0:
            self.weighted_price_sum += price * size
            self.total_size += size

    @property
    def price(self) -> float:
        # 成交量加权能降低小额离群成交的影响；零成交量数据退化为普通均价。
        if self.total_size > 0:
            return self.weighted_price_sum / self.total_size
        if self.trade_count > 0:
            return self.price_sum / self.trade_count
        # 无成交的秒没有新的价格发现，市场价格仍停留在最后一笔成交。
        assert self.last_price is not None
        return self.last_price


@dataclass(slots=True)
class _LiveBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_candle(cls, candle: Candle) -> _LiveBar:
        return cls(candle.timestamp, candle.open, candle.high, candle.low, candle.close)

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

    def to_candle(self) -> Candle:
        return Candle(self.timestamp, self.open, self.high, self.low, self.close)


@dataclass(slots=True)
class _SymbolState:
    atr: WilderAtr
    volume_24h_quote: float
    buckets: deque[_SecondBucket] = field(default_factory=deque)
    live_bar: _LiveBar | None = None
    last_tick_time: datetime | None = None
    candidate_direction: Literal["surge", "drop"] | None = None
    candidate_seconds: int = 0
    candidate_last_second: datetime | None = None
    # 断线期间的成交永久缺失，实时 K 线无法自行修复，必须等 REST 回补后才能再用 ATR 判定。
    atr_stale: bool = False


class AtrMoveDetector:
    def __init__(
        self,
        *,
        atr_period: int,
        candle_interval_seconds: int,
        lookback_seconds: int,
        trigger_atr_multiple: float,
        min_change_percent: float,
        confirmation_seconds: int,
        min_window_trades: int,
        max_atr_age_seconds: int,
        cooldown_seconds: int,
    ) -> None:
        self.atr_period = atr_period
        self.candle_interval_seconds = candle_interval_seconds
        self.lookback_seconds = lookback_seconds
        self.trigger_atr_multiple = trigger_atr_multiple
        self.min_change_percent = min_change_percent
        self.confirmation_seconds = confirmation_seconds
        self.min_window_trades = min_window_trades
        self.max_atr_age = timedelta(seconds=max_atr_age_seconds)
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self._candle_interval = timedelta(seconds=candle_interval_seconds)
        self._states: dict[str, _SymbolState] = {}
        self._last_alert: dict[str, datetime] = {}

    @property
    def symbols(self) -> list[str]:
        return sorted(self._states)

    @property
    def stale_symbols(self) -> list[str]:
        return sorted(symbol for symbol, state in self._states.items() if state.atr_stale)

    def add_symbol(
        self,
        symbol: str,
        candles: list[Candle],
        volume_24h_quote: float,
        live_candle: Candle | None = None,
    ) -> None:
        normalized = symbol.upper()
        existing = self._states.get(normalized)
        if existing is not None:
            existing.volume_24h_quote = volume_24h_quote
            return
        atr = WilderAtr(self.atr_period)
        atr.seed(candles)
        state = _SymbolState(atr=atr, volume_24h_quote=volume_24h_quote)
        if live_candle is not None and (atr.last_timestamp is None or live_candle.timestamp > atr.last_timestamp):
            # 订阅前当前 K 线已走完一段，用 REST 的未收盘 K 线起步，避免只含订阅后成交的残缺 K 线低估 ATR。
            state.live_bar = _LiveBar.from_candle(live_candle)
        self._states[normalized] = state

    def remove_symbols(self, symbols: set[str]) -> None:
        # 冷却记录刻意保留：合约被移出后很快重新入池时，不应绕过冷却再次提醒同一段行情。
        for symbol in symbols:
            self._states.pop(symbol, None)

    def mark_stream_gap(self) -> None:
        """行情中断后调用：丢弃秒级窗口与确认进度，并暂停 ATR 判定直到 resync_symbol 回补 K 线。"""
        for state in self._states.values():
            state.buckets.clear()
            self._reset_candidate(state)
            state.atr_stale = True

    def resync_symbol(
        self,
        symbol: str,
        candles: list[Candle],
        live_candle: Candle | None,
        fetched_at: datetime,
    ) -> bool:
        """用 REST K 线重建 ATR 与实时 K 线；合约已移除或已不需要回补时返回 False。"""
        state = self._states.get(symbol.upper())
        if state is None or not state.atr_stale:
            # 回补期间合约可能被移出又以全新预热状态重新入池，此时不能用这批数据覆盖。
            return False

        seed = sorted(candles, key=lambda item: item.timestamp)
        live_bar = state.live_bar
        if live_candle is not None:
            if live_bar is None or live_bar.timestamp < live_candle.timestamp:
                # 本地实时 K 线停留在断线前的周期，整体换成交易所的当前 K 线。
                live_bar = _LiveBar.from_candle(live_candle)
            elif live_bar.timestamp == live_candle.timestamp:
                # 本地 K 线含有请求返回后才到达的成交，交易所 K 线含有断线期间的成交，两者取并集才完整。
                is_local_newer = state.last_tick_time is not None and state.last_tick_time > fetched_at
                live_bar = _LiveBar(
                    live_candle.timestamp,
                    live_candle.open,
                    max(live_bar.high, live_candle.high),
                    min(live_bar.low, live_candle.low),
                    live_bar.close if is_local_newer else live_candle.close,
                )
            else:
                # 请求返回后本地已跨入新周期，交易所返回的“当前 K 线”其实已经收盘，应计入 ATR。
                seed.append(live_candle)

        atr = WilderAtr(self.atr_period)
        atr.seed(candle for candle in seed if live_bar is None or candle.timestamp < live_bar.timestamp)
        state.atr = atr
        state.live_bar = live_bar
        state.atr_stale = False
        return True

    def add_tick(self, tick: PriceTick) -> list[PriceAlert]:
        symbol = tick.symbol.upper()
        state = self._states.get(symbol)
        if state is None:
            return []

        timestamp = tick.timestamp.astimezone(UTC)
        if state.last_tick_time is not None and timestamp < state.last_tick_time:
            # 乱序旧价容易制造虚假的瞬时位移，因此不回填实时检测窗口。
            return []
        state.last_tick_time = timestamp
        self._update_bar(state, tick.price, timestamp)

        second = timestamp.replace(microsecond=0)
        if state.buckets and state.buckets[-1].timestamp == second:
            state.buckets[-1].add(tick.price, tick.size)
            return []

        alerts: list[PriceAlert] = []
        if state.buckets:
            completed = state.buckets[-1]
            missing_seconds = int((second - completed.timestamp).total_seconds()) - 1
            # 等一秒结束后再使用整秒 VWAP，避免把新一秒的第一笔成交误当成稳定价格。
            # 空档后才结算的秒，其 VWAP 已是几秒甚至几十秒前的旧价格，必须用当前成交价复核；
            # 紧邻结算只迟一秒，不复核，以免新一秒的单笔离群成交否掉本该发出的提醒。
            self._settle(symbol, state, completed, timestamp, alerts, tick.price if missing_seconds > 0 else None)
            # 稀疏合约在快速行情中常有空秒，沿用最后成交价补齐才能完成连续确认；
            # 空档超过观察窗口说明行情停滞，补齐已无比较意义。
            if 0 < missing_seconds <= self.lookback_seconds:
                last_price = completed.last_price
                assert last_price is not None
                for offset in range(1, missing_seconds + 1):
                    filler = _SecondBucket.carried(completed.timestamp + timedelta(seconds=offset), last_price)
                    state.buckets.append(filler)
                    # 补齐桶本就不能触发提醒，无需复核。
                    self._settle(symbol, state, filler, timestamp, alerts, None)

        current = _SecondBucket(second)
        current.add(tick.price, tick.size)
        state.buckets.append(current)
        return alerts

    def _settle(
        self,
        symbol: str,
        state: _SymbolState,
        bucket: _SecondBucket,
        timestamp: datetime,
        alerts: list[PriceAlert],
        latest_price: float | None,
    ) -> None:
        oldest_required = bucket.timestamp - timedelta(seconds=self.lookback_seconds)
        while len(state.buckets) > 2 and state.buckets[1].timestamp <= oldest_required:
            state.buckets.popleft()
        alert = self._evaluate(symbol, state, bucket, timestamp, latest_price)
        if alert is not None:
            alerts.append(alert)

    def _update_bar(self, state: _SymbolState, price: float, timestamp: datetime) -> None:
        bar_timestamp = self._floor_time(timestamp)
        if state.live_bar is None:
            state.live_bar = _LiveBar(bar_timestamp, price, price, price, price)
            return
        if bar_timestamp == state.live_bar.timestamp:
            state.live_bar.update(price)
            return
        if bar_timestamp > state.live_bar.timestamp:
            if not state.atr_stale:
                # 断线期间 ATR 等待 REST 重建，此时喂入缺口两侧的 K 线只会引入错误数据。
                previous = state.live_bar.to_candle()
                state.atr.update(previous)
                self._fill_flat_bars(state, previous, bar_timestamp)
            state.live_bar = _LiveBar(bar_timestamp, price, price, price, price)

    def _fill_flat_bars(self, state: _SymbolState, previous: Candle, next_bar: datetime) -> None:
        # 连接正常时跨过的周期确实无人成交；Gate 对这类周期返回开高低收都等于上一收盘价的平线 K 线，
        # 实时计算保持同样口径，ATR 才与预热数据一致，且冷门合约停摆后 ATR 时间戳不会被误判为过期。
        missing = int((next_bar - previous.timestamp) / self._candle_interval) - 1
        if missing <= 0:
            return
        # 连续平线让 Wilder ATR 按 (n-1)/n 几何衰减，4n 根后原值只剩约 2%，更早的平线无需逐根计算。
        count = min(missing, self.atr_period * 4)
        for offset in range(count, 0, -1):
            flat_time = next_bar - self._candle_interval * offset
            state.atr.update(Candle(flat_time, previous.close, previous.close, previous.close, previous.close))

    def _floor_time(self, timestamp: datetime) -> datetime:
        epoch = int(timestamp.timestamp())
        floored = epoch - (epoch % self.candle_interval_seconds)
        return datetime.fromtimestamp(floored, tz=UTC)

    def _evaluate(
        self,
        symbol: str,
        state: _SymbolState,
        current: _SecondBucket,
        timestamp: datetime,
        latest_price: float | None,
    ) -> PriceAlert | None:
        """评估一个已结束的秒；latest_price 非空时还需该价格仍满足门槛才允许提醒。"""
        atr = state.atr.value
        atr_timestamp = state.atr.last_timestamp
        if state.atr_stale or atr is None or atr <= 0 or atr_timestamp is None:
            self._reset_candidate(state)
            return None
        # K 线时间戳是开盘时间，按收盘时间计算年龄，配置值才等于“ATR 最近一次更新距今多久”。
        if timestamp - (atr_timestamp + self._candle_interval) > self.max_atr_age:
            self._reset_candidate(state)
            return None

        target = current.timestamp - timedelta(seconds=self.lookback_seconds)
        baseline = next((bucket for bucket in reversed(state.buckets) if bucket.timestamp <= target), None)
        if baseline is None:
            self._reset_candidate(state)
            return None
        observed_seconds = (current.timestamp - baseline.timestamp).total_seconds()
        if observed_seconds > self.lookback_seconds + 2:
            self._reset_candidate(state)
            return None

        trade_count = sum(
            bucket.trade_count
            for bucket in state.buckets
            if baseline.timestamp < bucket.timestamp <= current.timestamp
        )
        if trade_count < self.min_window_trades:
            self._reset_candidate(state)
            return None

        price_move = current.price - baseline.price
        if not self._exceeds_thresholds(price_move, baseline.price, atr):
            self._reset_candidate(state)
            return None

        direction = "surge" if price_move > 0 else "drop"
        if not self._confirm_candidate(state, direction, current.timestamp):
            return None
        if not current.has_trades:
            # 补齐的空秒只延续确认进度，不能单独触发：否则一笔离群成交后恰好无人成交也会被当成持续异动。
            return None
        if latest_price is not None and not self._still_moving(latest_price, baseline.price, atr, direction):
            # 这一秒是在空档之后才被结算的，它的价格已经过期，而触发结算的那笔成交是此刻唯一的
            # 价格证据：它若已不满足门槛，说明异动在空档中就结束了，再按旧价格提醒只会误导。
            # 同样只拦提醒、不重置确认进度，与上面补齐桶的处理保持一致。
            return None

        previous_alert = self._last_alert.get(symbol)
        if previous_alert is not None and timestamp - previous_alert < self.cooldown:
            self._reset_candidate(state)
            return None
        self._last_alert[symbol] = timestamp
        self._reset_candidate(state)

        return PriceAlert(
            symbol=symbol,
            direction=direction,
            price=current.price,
            reference_price=baseline.price,
            change_percent=price_move / baseline.price * 100.0,
            move_atr=abs(price_move) / atr,
            atr=atr,
            atr_period=self.atr_period,
            lookback_seconds=self.lookback_seconds,
            trade_count=trade_count,
            volume_24h_quote=state.volume_24h_quote,
            timestamp=timestamp,
        )

    def _exceeds_thresholds(self, price_move: float, baseline_price: float, atr: float) -> bool:
        # 两道门槛必须同时满足：百分比保证肉眼可感知，ATR 倍数适配不同市场波动率。
        # 判定与空档后的复核共用这一份规则，避免两处阈值逐渐走偏。
        return (
            abs(price_move / baseline_price * 100.0) >= self.min_change_percent
            and abs(price_move) / atr >= self.trigger_atr_multiple
        )

    def _still_moving(
        self,
        latest_price: float,
        baseline_price: float,
        atr: float,
        direction: Literal["surge", "drop"],
    ) -> bool:
        price_move = latest_price - baseline_price
        # 反向必须单独判断：仅看幅度的话，急涨过后直接砸穿基准价也能满足门槛而发出急涨提醒。
        if (price_move > 0) != (direction == "surge"):
            return False
        return self._exceeds_thresholds(price_move, baseline_price, atr)

    def _confirm_candidate(
        self,
        state: _SymbolState,
        direction: Literal["surge", "drop"],
        second: datetime,
    ) -> bool:
        is_consecutive = (
            state.candidate_direction == direction
            and state.candidate_last_second is not None
            and second - state.candidate_last_second == timedelta(seconds=1)
        )
        state.candidate_seconds = state.candidate_seconds + 1 if is_consecutive else 1
        state.candidate_direction = direction
        state.candidate_last_second = second
        return state.candidate_seconds >= self.confirmation_seconds

    @staticmethod
    def _reset_candidate(state: _SymbolState) -> None:
        state.candidate_direction = None
        state.candidate_seconds = 0
        state.candidate_last_second = None
