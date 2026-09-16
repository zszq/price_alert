from datetime import UTC, datetime, timedelta

from price_alert.detector import AtrMoveDetector
from price_alert.models import Candle, PriceTick

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def history(range_size: float, period: int = 3) -> list[Candle]:
    return [
        Candle(
            BASE + timedelta(minutes=index),
            100.0,
            100.0 + range_size / 2,
            100.0 - range_size / 2,
            100.0,
        )
        for index in range(period)
    ]


def detector() -> AtrMoveDetector:
    return AtrMoveDetector(
        atr_period=3,
        candle_interval_seconds=60,
        lookback_seconds=10,
        trigger_atr_multiple=0.8,
        min_window_trades=2,
        max_atr_age_seconds=180,
        cooldown_seconds=120,
    )


def feed_window(instance: AtrMoveDetector, symbol: str, final_price: float):
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick(symbol, 100.0, 1.0, start + timedelta(seconds=second), str(second)))
    return instance.add_tick(PriceTick(symbol, final_price, 1.0, start + timedelta(seconds=11), "final"))


def test_same_price_move_triggers_in_low_atr_but_not_high_atr_market():
    instance = detector()
    instance.add_symbol("LOW_USDT", history(1.0), 20_000_000)
    instance.add_symbol("HIGH_USDT", history(4.0), 30_000_000)

    low_alerts = feed_window(instance, "LOW_USDT", 101.0)
    high_alerts = feed_window(instance, "HIGH_USDT", 101.0)

    assert len(low_alerts) == 1
    assert low_alerts[0].move_atr == 1.0
    assert high_alerts == []


def test_cooldown_is_separate_for_surge_and_drop():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    assert feed_window(instance, "BTC_USDT", 101.0)[0].direction == "surge"

    start = BASE + timedelta(minutes=3, seconds=12)
    alert = instance.add_tick(PriceTick("BTC_USDT", 99.0, 1.0, start, "drop"))
    assert alert[0].direction == "drop"


def test_ignores_unknown_and_out_of_order_ticks():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    current = BASE + timedelta(minutes=3)
    assert instance.add_tick(PriceTick("OTHER_USDT", 100, 1, current)) == []
    instance.add_tick(PriceTick("BTC_USDT", 100, 1, current))
    assert instance.add_tick(PriceTick("BTC_USDT", 80, 1, current - timedelta(seconds=1))) == []
