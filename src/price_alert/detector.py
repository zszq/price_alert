"""使用 ATR 标准化短时价格位移，识别动态异动。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from price_alert.indicators import WilderAtr
from price_alert.models import Candle, PriceAlert, PriceTick


@dataclass(slots=True)
class _SecondBucket:
    timestamp: datetime
    close: float
    trade_count: int = 1


@dataclass(slots=True)
class _LiveBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

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


class AtrMoveDetector:
    def __init__(
        self,
        *,
        atr_period: int,
        candle_interval_seconds: int,
        lookback_seconds: int,
        trigger_atr_multiple: float,
        min_window_trades: int,
        max_atr_age_seconds: int,
        cooldown_seconds: int,
    ) -> None:
        self.atr_period = atr_period
        self.candle_interval_seconds = candle_interval_seconds
        self.lookback_seconds = lookback_seconds
        self.trigger_atr_multiple = trigger_atr_multiple
        self.min_window_trades = min_window_trades
        self.max_atr_age = timedelta(seconds=max_atr_age_seconds)
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self._states: dict[str, _SymbolState] = {}
        self._last_alert: dict[tuple[str, str], datetime] = {}

    @property
    def symbols(self) -> list[str]:
        return sorted(self._states)

    def add_symbol(self, symbol: str, candles: list[Candle], volume_24h_quote: float) -> None:
        normalized = symbol.upper()
        existing = self._states.get(normalized)
        if existing is not None:
            existing.volume_24h_quote = volume_24h_quote
            return
        atr = WilderAtr(self.atr_period)
        atr.seed(candles)
        self._states[normalized] = _SymbolState(atr=atr, volume_24h_quote=volume_24h_quote)

    def remove_symbols(self, symbols: set[str]) -> None:
        for symbol in symbols:
            self._states.pop(symbol, None)
        self._last_alert = {
            key: timestamp for key, timestamp in self._last_alert.items() if key[0] not in symbols
        }

    def add_tick(self, tick: PriceTick) -> list[PriceAlert]:
        state = self._states.get(tick.symbol.upper())
        if state is None:
            return []

        timestamp = tick.timestamp.astimezone(UTC)
        if state.last_tick_time is not None and timestamp < state.last_tick_time:
            # 乱序旧价容易制造虚假的瞬时位移，因此不回填实时检测窗口。
            return []
        state.last_tick_time = timestamp
        self._update_bar(state, tick.price, timestamp)

        second = timestamp.replace(microsecond=0)
        is_new_second = not state.buckets or state.buckets[-1].timestamp != second
        if is_new_second:
            state.buckets.append(_SecondBucket(second, tick.price))
        else:
            state.buckets[-1].close = tick.price
            state.buckets[-1].trade_count += 1

        oldest_required = second - timedelta(seconds=self.lookback_seconds)
        while len(state.buckets) > 1 and state.buckets[1].timestamp <= oldest_required:
            state.buckets.popleft()

        if not is_new_second:
            return []
        alert = self._evaluate(tick.symbol.upper(), state, timestamp)
        return [alert] if alert is not None else []

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

    def _evaluate(self, symbol: str, state: _SymbolState, timestamp: datetime) -> PriceAlert | None:
        atr = state.atr.value
        atr_timestamp = state.atr.last_timestamp
        if atr is None or atr <= 0 or atr_timestamp is None:
            return None
        if timestamp - atr_timestamp > self.max_atr_age:
            return None

        current = state.buckets[-1]
        target = current.timestamp - timedelta(seconds=self.lookback_seconds)
        baseline = next((bucket for bucket in reversed(state.buckets) if bucket.timestamp <= target), None)
        if baseline is None:
            return None
        observed_seconds = (current.timestamp - baseline.timestamp).total_seconds()
        if observed_seconds > self.lookback_seconds + 2:
            return None

        trade_count = sum(
            bucket.trade_count
            for bucket in state.buckets
            if baseline.timestamp < bucket.timestamp <= current.timestamp
        )
        if trade_count < self.min_window_trades:
            return None

        price_move = current.close - baseline.close
        move_atr = abs(price_move) / atr
        if move_atr < self.trigger_atr_multiple:
            return None

        direction = "surge" if price_move > 0 else "drop"
        cooldown_key = (symbol, direction)
        previous_alert = self._last_alert.get(cooldown_key)
        if previous_alert is not None and timestamp - previous_alert < self.cooldown:
            return None
        self._last_alert[cooldown_key] = timestamp

        return PriceAlert(
            symbol=symbol,
            direction=direction,
            price=current.close,
            reference_price=baseline.close,
            change_percent=(current.close / baseline.close - 1.0) * 100.0,
            move_atr=move_atr,
            atr=atr,
            atr_period=self.atr_period,
            lookback_seconds=self.lookback_seconds,
            trade_count=trade_count,
            volume_24h_quote=state.volume_24h_quote,
            timestamp=timestamp,
        )
