import json

from price_alert.gate import build_subscription, parse_candle, parse_trade_message


def test_builds_gate_trade_subscription():
    request = json.loads(build_subscription(["BTC_USDT", "ETH_USDT"]))

    assert request["channel"] == "futures.trades"
    assert request["event"] == "subscribe"
    assert request["payload"] == ["BTC_USDT", "ETH_USDT"]


def test_parses_candle_and_filters_internal_trades():
    candle = parse_candle({"t": 1_700_000_000, "o": "100", "h": "102", "l": "99", "c": "101", "sum": "5000"})
    assert candle.high == 102
    assert candle.quote_volume == 5000

    message = json.dumps(
        {
            "channel": "futures.trades",
            "event": "update",
            "result": [
                {"id": 1, "contract": "BTC_USDT", "price": "70000", "size": "-2", "create_time_ms": 1_700_000_000_123},
                {
                    "id": 2,
                    "contract": "BTC_USDT",
                    "price": "1",
                    "size": "1",
                    "create_time_ms": 1_700_000_000_124,
                    "is_internal": True,
                },
            ],
        }
    )

    ticks = parse_trade_message(message)

    assert len(ticks) == 1
    assert ticks[0].price == 70000
    assert ticks[0].size == 2
