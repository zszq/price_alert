from datetime import UTC, datetime, timedelta

import pytest

from price_alert.indicators import WilderAtr
from price_alert.models import Candle


def candle(minute: int, high: float, low: float, close: float) -> Candle:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)
    return Candle(timestamp, close, high, low, close)


def test_wilder_atr_warmup_and_recursive_update():
    atr = WilderAtr(period=3)
    atr.seed([candle(0, 101, 99, 100), candle(1, 101, 99, 100), candle(2, 101, 99, 100)])

    assert atr.ready
    assert atr.value == 2.0

    atr.update(candle(3, 104, 100, 103))
    assert atr.value == pytest.approx((2 * 2 + 4) / 3)


def test_wilder_atr_ignores_duplicate_or_old_candle():
    atr = WilderAtr(period=2)
    atr.seed([candle(0, 101, 99, 100), candle(1, 101, 99, 100)])

    assert atr.update(candle(1, 120, 80, 100)) == 2.0
