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


def feed_window_at(instance: AtrMoveDetector, symbol: str, final_price: float, start):
    alerts = []
    for second in range(11):
        instance.add_tick(PriceTick(symbol, 100.0, 1.0, start + timedelta(seconds=second)))
    for second in range(11, 14):
        alerts.extend(instance.add_tick(PriceTick(symbol, final_price, 1.0, start + timedelta(seconds=second))))
    return alerts


def test_sparse_trades_are_confirmed_by_carrying_last_price_through_empty_seconds():
    instance = detector()
    instance.add_symbol("THIN_USDT", history(1.0), 20_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("THIN_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    # 每隔一秒才有一笔成交：旧逻辑因“相邻秒”被打断而永远无法确认。
    assert instance.add_tick(PriceTick("THIN_USDT", 101.0, 1.0, start + timedelta(seconds=11))) == []
    assert instance.add_tick(PriceTick("THIN_USDT", 101.0, 1.0, start + timedelta(seconds=13))) == []
    alerts = instance.add_tick(PriceTick("THIN_USDT", 101.0, 1.0, start + timedelta(seconds=15)))

    assert len(alerts) == 1
    assert alerts[0].direction == "surge"


def test_single_wick_followed_by_silence_does_not_alert_from_carried_seconds():
    instance = detector()
    instance.add_symbol("THIN_USDT", history(1.0), 20_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("THIN_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    alerts = []
    alerts.extend(instance.add_tick(PriceTick("THIN_USDT", 101.0, 1.0, start + timedelta(seconds=11))))
    # 空秒 12、13 延续了确认进度，但价格在下一笔真实成交时已经回落，不能提醒。
    alerts.extend(instance.add_tick(PriceTick("THIN_USDT", 100.0, 1.0, start + timedelta(seconds=14))))
    alerts.extend(instance.add_tick(PriceTick("THIN_USDT", 100.0, 1.0, start + timedelta(seconds=15))))

    assert alerts == []


def test_gap_longer_than_lookback_is_not_filled():
    instance = detector()
    instance.add_symbol("THIN_USDT", history(1.0), 20_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("THIN_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    alerts = []
    for second in (40, 41, 42):
        alerts.extend(instance.add_tick(PriceTick("THIN_USDT", 101.0, 1.0, start + timedelta(seconds=second))))

    assert alerts == []


def test_reset_realtime_prevents_comparing_prices_across_disconnect():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    instance.reset_realtime()
    alerts = []
    for second in range(11, 15):
        alerts.extend(instance.add_tick(PriceTick("BTC_USDT", 101.0, 1.0, start + timedelta(seconds=second))))

    assert alerts == []


def test_atr_age_is_measured_from_candle_close():
    fresh = detector()
    fresh.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    # 最后一根种子 K 线 02:00 开盘、03:00 收盘；05:10 起的窗口距收盘不到 180 秒，仍应判定。
    assert len(feed_window_at(fresh, "BTC_USDT", 101.0, BASE + timedelta(minutes=5, seconds=10))) == 1

    stale = detector()
    stale.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    assert feed_window_at(stale, "BTC_USDT", 101.0, BASE + timedelta(minutes=6, seconds=10)) == []


def test_live_candle_from_warmup_is_included_in_next_atr_update():
    with_live = detector()
    live_candle = Candle(BASE + timedelta(minutes=3), 100.0, 106.0, 100.0, 100.0)
    with_live.add_symbol("BTC_USDT", history(1.0), 1_000_000_000, live_candle)
    without_live = detector()
    without_live.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)

    start = BASE + timedelta(minutes=4)
    # 订阅前的 6 点振幅计入 ATR 后，1% 位移不再足够异常。
    assert feed_window_at(with_live, "BTC_USDT", 101.0, start) == []
    assert len(feed_window_at(without_live, "BTC_USDT", 101.0, start)) == 1


def test_cooldown_survives_symbol_leaving_and_rejoining_universe():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    assert len(feed_window(instance, "BTC_USDT", 101.0)) == 1

    instance.remove_symbols({"BTC_USDT"})
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)

    assert feed_window_at(instance, "BTC_USDT", 99.0, BASE + timedelta(minutes=3, seconds=20)) == []
