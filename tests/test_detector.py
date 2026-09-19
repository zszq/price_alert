from datetime import UTC, datetime, timedelta

import pytest

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


def stale_second_settled_after_gap(settling_price: float) -> list:
    """构造“异动秒之后出现空档，由一笔迟到的成交来结算它”的场景。

    确认进度在空档中由补齐桶凑满，因此提醒只能由空档前那个真实秒产生，
    而它的价格此时已经是 8 秒前的旧值。
    """
    instance = detector()
    instance.add_symbol("THIN_USDT", history(1.0), 20_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("THIN_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    alerts = []
    for second in (11, 12):
        alerts.extend(instance.add_tick(PriceTick("THIN_USDT", 101.0, 1.0, start + timedelta(seconds=second))))
    assert alerts == []
    return instance.add_tick(PriceTick("THIN_USDT", settling_price, 1.0, start + timedelta(seconds=20)))


def test_stale_second_is_not_alerted_when_settling_trade_shows_the_move_is_over():
    # 结算这一秒的成交已经回到 100，异动在空档中就结束了，不能再按 101 发提醒。
    assert stale_second_settled_after_gap(100.0) == []


def test_stale_second_is_still_alerted_when_settling_trade_confirms_the_move():
    # 同一条路径上价格仍在 101：空档后结算是稀疏合约唯一的提醒时机，不能一并拦掉。
    alerts = stale_second_settled_after_gap(101.0)

    assert len(alerts) == 1
    assert alerts[0].direction == "surge"
    assert alerts[0].price == 101.0


def test_reversal_after_gap_does_not_alert_in_the_old_direction():
    # 复核必须看方向：砸穿基准价的成交在幅度上同样超标，只比幅度会发出方向相反的提醒。
    assert stale_second_settled_after_gap(98.0) == []


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


def test_stream_gap_prevents_comparing_prices_across_disconnect():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    start = BASE + timedelta(minutes=3)
    for second in range(11):
        instance.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, start + timedelta(seconds=second)))

    instance.mark_stream_gap()
    instance.resync_symbol("BTC_USDT", history(1.0), None, start + timedelta(seconds=10))
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


def atr_of(instance: AtrMoveDetector, symbol: str):
    # ATR 没有公开读取接口；K 线口径问题直接检查内部 ATR 最清楚，也不必借助整条提醒链路间接推断。
    return instance._states[symbol].atr


def test_stale_atr_pauses_detection_until_resynced():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    instance.mark_stream_gap()
    start = BASE + timedelta(minutes=3)

    assert instance.stale_symbols == ["BTC_USDT"]
    assert feed_window_at(instance, "BTC_USDT", 101.0, start) == []

    assert instance.resync_symbol("BTC_USDT", history(1.0), None, start + timedelta(seconds=14)) is True
    assert instance.stale_symbols == []
    assert len(feed_window_at(instance, "BTC_USDT", 101.0, start + timedelta(seconds=20))) == 1


def test_resync_ignores_unknown_or_already_fresh_symbols():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)

    assert instance.resync_symbol("BTC_USDT", history(4.0), None, BASE) is False
    assert instance.resync_symbol("ETH_USDT", history(4.0), None, BASE) is False
    assert atr_of(instance, "BTC_USDT").value == 1.0


def test_resync_merges_local_and_exchange_view_of_current_bar():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    bar = BASE + timedelta(minutes=3)
    # 断线前本地见到了 103 的高点，断线期间交易所记录了 96 的低点。
    instance.add_tick(PriceTick("BTC_USDT", 103.0, 1.0, bar + timedelta(seconds=5)))
    instance.mark_stream_gap()
    instance.add_tick(PriceTick("BTC_USDT", 98.0, 1.0, bar + timedelta(seconds=50)))

    exchange_bar = Candle(bar, 100.0, 101.0, 96.0, 97.0)
    assert instance.resync_symbol("BTC_USDT", history(1.0), exchange_bar, bar + timedelta(seconds=40))
    instance.add_tick(PriceTick("BTC_USDT", 98.0, 1.0, bar + timedelta(seconds=60)))

    atr = atr_of(instance, "BTC_USDT")
    # 合并 K 线高 103、低 96，TR=7，ATR=(1×2+7)/3。
    assert atr.last_timestamp == bar
    assert atr.value == pytest.approx(3.0)


def test_resync_counts_exchange_current_bar_as_closed_after_local_rollover():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    instance.mark_stream_gap()
    bar = BASE + timedelta(minutes=3)
    instance.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar + timedelta(seconds=61)))

    assert instance.resync_symbol(
        "BTC_USDT",
        history(1.0),
        Candle(bar, 100.0, 104.0, 100.0, 100.0),
        bar + timedelta(seconds=59),
    )

    atr = atr_of(instance, "BTC_USDT")
    assert atr.last_timestamp == bar
    assert atr.value == pytest.approx((1.0 * 2 + 4.0) / 3)


def test_bars_without_trades_are_filled_with_flat_candles_like_gate():
    instance = detector()
    instance.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    bar = BASE + timedelta(minutes=3)
    instance.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar))
    instance.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar + timedelta(minutes=3)))

    atr = atr_of(instance, "BTC_USDT")
    # 03:00 的实时 K 线与 04:00、05:00 两根平线 TR 均为 0，ATR 每根衰减为 2/3。
    assert atr.last_timestamp == BASE + timedelta(minutes=5)
    assert atr.value == pytest.approx((2 / 3) ** 3)


def test_flat_fill_is_capped_and_skipped_while_stale():
    bar = BASE + timedelta(minutes=3)
    capped = detector()
    capped.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    capped.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar))
    capped.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar + timedelta(minutes=1000)))
    assert atr_of(capped, "BTC_USDT").last_timestamp == bar + timedelta(minutes=999)

    stale = detector()
    stale.add_symbol("BTC_USDT", history(1.0), 1_000_000_000)
    stale.mark_stream_gap()
    stale.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar))
    stale.add_tick(PriceTick("BTC_USDT", 100.0, 1.0, bar + timedelta(minutes=3)))
    assert atr_of(stale, "BTC_USDT").last_timestamp == BASE + timedelta(minutes=2)
