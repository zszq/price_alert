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
        min_change_percent=1.0,
        confirmation_seconds=2,
        min_window_trades=2,
        max_atr_age_seconds=180,
        cooldown_seconds=120,
    )


def feed_window(instance: AtrMoveDetector, symbol: str, final_price: float):
    start = BASE + timedelta(minutes=3)
    alerts = []
    for second in range(11):
        instance.add_tick(PriceTick(symbol, 100.0, 1.0, start + timedelta(seconds=second), str(second)))
    for second in range(11, 14):
        alerts.extend(
            instance.add_tick(PriceTick(symbol, final_price, 1.0, start + timedelta(seconds=second), str(second)))
        )
    return alerts


def test_same_price_move_triggers_in_low_atr_but_not_high_atr_market():
    instance = detector()
    instance.add_symbol("LOW_USDT", history(1.0), 20_000_000)
    instance.add_symbol("HIGH_USDT", history(4.0), 30_000_000)

    low_alerts = feed_window(instance, "LOW_USDT", 101.0)
    high_alerts = feed_window(instance, "HIGH_USDT", 101.0)

    assert len(low_alerts) == 1
    assert low_alerts[0].move_atr == 1.0
    assert high_alerts == []


def test_rejects_move_below_minimum_percent_even_when_atr_multiple_is_high():
    instance = detector()
    instance.add_symbol("LOW_USDT", history(0.2), 20_000_000)

    alerts = feed_window(instance, "LOW_USDT", 100.9)

    assert alerts == []


def test_cooldown_is_shared_between_surge_and_drop():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    assert feed_window(instance, "BTC_USDT", 101.0)[0].direction == "surge"

    alerts = []
    start = BASE + timedelta(minutes=3)
    for second in range(14, 17):
        alerts.extend(instance.add_tick(PriceTick("BTC_USDT", 99.0, 1.0, start + timedelta(seconds=second))))

    assert alerts == []


def test_requires_consecutive_completed_seconds_before_alerting():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    assert instance.add_tick(PriceTick("BTC_USDT", 101.0, 1.0, start + timedelta(seconds=11))) == []
    assert instance.add_tick(PriceTick("BTC_USDT", 101.0, 1.0, start + timedelta(seconds=12))) == []
    alerts = instance.add_tick(PriceTick("BTC_USDT", 101.0, 1.0, start + timedelta(seconds=13)))

    assert len(alerts) == 1


def test_small_outlier_trade_is_diluted_by_completed_second_vwap():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("BTC_USDT", 100.0, 10.0, start + timedelta(seconds=second)))

    outlier_time = start + timedelta(seconds=11)
    assert instance.add_tick(PriceTick("BTC_USDT", 110.0, 0.01, outlier_time)) == []
    instance.add_tick(PriceTick("BTC_USDT", 100.0, 10.0, outlier_time + timedelta(milliseconds=100)))
    alerts = instance.add_tick(PriceTick("BTC_USDT", 100.0, 10.0, start + timedelta(seconds=12)))

    assert alerts == []


def test_ignores_unknown_and_out_of_order_ticks():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    current = BASE + timedelta(minutes=3)
    assert instance.add_tick(PriceTick("OTHER_USDT", 100, 1, current)) == []
    instance.add_tick(PriceTick("BTC_USDT", 100, 1, current))
    assert instance.add_tick(PriceTick("BTC_USDT", 80, 1, current - timedelta(seconds=1))) == []
