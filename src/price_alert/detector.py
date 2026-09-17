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

    def reset_realtime(self) -> None:
        """行情中断后调用：丢弃秒级窗口与确认进度，避免把断线前后的价格当作连续行情比较。"""
        for state in self._states.values():
            state.buckets.clear()
            self._reset_candidate(state)

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
            # 等一秒结束后再使用整秒 VWAP，避免把新一秒的第一笔成交误当成稳定价格。
            self._settle(symbol, state, completed, timestamp, alerts)
            missing_seconds = int((second - completed.timestamp).total_seconds()) - 1
            # 稀疏合约在快速行情中常有空秒，沿用最后成交价补齐才能完成连续确认；
            # 空档超过观察窗口说明行情停滞，补齐已无比较意义。
            if 0 < missing_seconds <= self.lookback_seconds:
                last_price = completed.last_price
                assert last_price is not None
                for offset in range(1, missing_seconds + 1):
                    filler = _SecondBucket.carried(completed.timestamp + timedelta(seconds=offset), last_price)
                    state.buckets.append(filler)
                    self._settle(symbol, state, filler, timestamp, alerts)

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
    ) -> None:
        oldest_required = bucket.timestamp - timedelta(seconds=self.lookback_seconds)
        while len(state.buckets) > 2 and state.buckets[1].timestamp <= oldest_required:
            state.buckets.popleft()
        alert = self._evaluate(symbol, state, bucket, timestamp)
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
            state.atr.update(state.live_bar.to_candle())
            state.live_bar = _LiveBar(bar_timestamp, price, price, price, price)

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
    ) -> PriceAlert | None:
        atr = state.atr.value
        atr_timestamp = state.atr.last_timestamp
        if atr is None or atr <= 0 or atr_timestamp is None:
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
        change_percent = price_move / baseline.price * 100.0
        move_atr = abs(price_move) / atr
        # 两道门槛必须同时满足：百分比保证肉眼可感知，ATR 倍数适配不同市场波动率。
        if abs(change_percent) < self.min_change_percent or move_atr < self.trigger_atr_multiple:
            self._reset_candidate(state)
            return None

        direction = "surge" if price_move > 0 else "drop"
        if not self._confirm_candidate(state, direction, current.timestamp):
            return None
        if not current.has_trades:
            # 补齐的空秒只延续确认进度，不能单独触发：否则一笔离群成交后恰好无人成交也会被当成持续异动。
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
            change_percent=change_percent,
            move_atr=move_atr,
            atr=atr,
            atr_period=self.atr_period,
            lookback_seconds=self.lookback_seconds,
            trade_count=trade_count,
            volume_24h_quote=state.volume_24h_quote,
            timestamp=timestamp,
        )

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
