"""无第三方数值依赖的 Wilder ATR。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from price_alert.models import Candle


class WilderAtr:
    def __init__(self, period: int = 14) -> None:
        if period < 2:
            raise ValueError("ATR period 必须至少为 2")
        self.period = period
        self.value: float | None = None
        self.last_timestamp: datetime | None = None
        self._previous_close: float | None = None
        self._warmup: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def seed(self, candles: Iterable[Candle]) -> None:
        for candle in sorted(candles, key=lambda item: item.timestamp):
            self.update(candle)

    def update(self, candle: Candle) -> float | None:
        if self.last_timestamp is not None and candle.timestamp <= self.last_timestamp:
            return self.value

        true_range = candle.high - candle.low
        if self._previous_close is not None:
            true_range = max(
                true_range,
                abs(candle.high - self._previous_close),
                abs(candle.low - self._previous_close),
            )

        if self.value is None:
            self._warmup.append(true_range)
            if len(self._warmup) == self.period:
                self.value = sum(self._warmup) / self.period
                self._warmup.clear()
        else:
            self.value = ((self.value * (self.period - 1)) + true_range) / self.period

        self._previous_close = candle.close
        self.last_timestamp = candle.timestamp
        return self.value
