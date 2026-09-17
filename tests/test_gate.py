import asyncio
import io
import json
import urllib.error

import pytest

from price_alert import gate
from price_alert.gate import (
    GateRestClient,
    GateTradeFeed,
    build_subscription,
    parse_candle,
    parse_trade_message,
    parse_trade_payload,
)


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


def trade(contract: str = "BTC_USDT", price: object = "100", **extra):
    return {"id": 1, "contract": contract, "price": price, "size": "1", "create_time_ms": 1_700_000_000_000} | extra


def update(*trades):
    return {"channel": "futures.trades", "event": "update", "result": list(trades)}


def test_skips_malformed_trades_without_dropping_valid_ones():
    payload = update(
        "not-a-dict",
        {"contract": "BTC_USDT", "size": "1", "create_time_ms": 1},
        trade(price="nan"),
        trade(price="-1"),
        trade(price="abc"),
        trade(create_time_ms=10**30),
        trade("ETH_USDT", "2000"),
    )

    ticks = parse_trade_payload(payload)

    assert [(tick.symbol, tick.price) for tick in ticks] == [("ETH_USDT", 2000.0)]


def test_ignores_update_without_trade_list():
    assert parse_trade_payload({"channel": "futures.trades", "event": "update", "result": {"status": "x"}}) == []


def test_subscription_request_carries_event_and_id():
    request = json.loads(build_subscription(["BTC_USDT"], "unsubscribe", 7))

    assert request["event"] == "unsubscribe"
    assert request["id"] == 7


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


class ResetResponse(FakeResponse):
    def read(self, *args):
        raise ConnectionResetError("reset while reading body")


def rest_with_responses(monkeypatch, responses):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        result = responses[len(calls) - 1]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gate.time, "sleep", lambda seconds: None)
    return GateRestClient("https://example.test", retries=3), calls


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.test", code, "error", {}, None)


def test_rest_does_not_retry_client_errors(monkeypatch):
    client, calls = rest_with_responses(monkeypatch, [http_error(400)])

    with pytest.raises(urllib.error.HTTPError):
        client.fetch_tickers()

    assert len(calls) == 1


def test_rest_retries_rate_limit_and_body_read_failures(monkeypatch):
    client, calls = rest_with_responses(
        monkeypatch,
        [http_error(429), ResetResponse(), FakeResponse(b"[]")],
    )

    assert client.fetch_tickers() == []
    assert len(calls) == 3


def test_rest_raises_after_exhausting_retries(monkeypatch):
    client, calls = rest_with_responses(monkeypatch, [http_error(503)] * 3)

    with pytest.raises(urllib.error.HTTPError):
        client.fetch_contracts()

    assert len(calls) == 3


class FakeWebSocket:
    def __init__(self, respond=None):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.respond = respond or (lambda request: [])

    async def send(self, message: str) -> None:
        request = json.loads(message)
        self.sent.append(request)
        for reply in self.respond(request):
            self.incoming.put_nowait(json.dumps(reply))

    async def recv(self) -> str:
        return await self.incoming.get()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


def ack(request, error=None):
    return {"channel": "futures.trades", "event": request["event"], "id": request["id"], "error": error}


def feed_for(websocket: FakeWebSocket, **kwargs) -> GateTradeFeed:
    return GateTradeFeed("wss://example.test", connect=lambda url, **options: websocket, **kwargs)


def test_feed_subscribes_in_chunks_and_skips_undecodable_messages():
    async def scenario():
        def respond(request):
            if request["payload"] == ["SOL_USDT"]:
                return [ack(request), update(trade("SOL_USDT"))]
            return [ack(request)]

        websocket = FakeWebSocket(respond)
        websocket.incoming.put_nowait("{not json")
        feed = feed_for(websocket, chunk_size=2)
        await feed.set_symbols(["sol_usdt", "BTC_USDT", "ETH_USDT"])
        stream = feed.stream()
        tick = await anext(stream)
        await stream.aclose()
        return websocket, tick

    websocket, tick = asyncio.run(scenario())

    assert [request["payload"] for request in websocket.sent] == [["BTC_USDT", "ETH_USDT"], ["SOL_USDT"]]
    assert tick.symbol == "SOL_USDT"


def test_failed_batch_subscription_is_retried_per_symbol_and_bad_symbol_is_reported():
    async def scenario():
        def respond(request):
            symbols = request["payload"]
            if len(symbols) > 1 or symbols == ["BAD_USDT"]:
                return [ack(request, {"code": 2, "message": "unknown contract"})]
            return [ack(request), update(trade(symbols[0]))]

        websocket = FakeWebSocket(respond)
        feed = feed_for(websocket)
        await feed.set_symbols(["BAD_USDT", "GOOD_USDT"])
        stream = feed.stream()
        tick = await anext(stream)
        await stream.aclose()
        return websocket, feed, tick

    websocket, feed, tick = asyncio.run(scenario())

    assert [request["payload"] for request in websocket.sent] == [
        ["BAD_USDT", "GOOD_USDT"],
        ["BAD_USDT"],
        ["GOOD_USDT"],
    ]
    assert feed.rejected_symbols == {"BAD_USDT"}
    assert tick.symbol == "GOOD_USDT"


def test_symbol_changes_are_applied_incrementally_while_connected():
    async def scenario():
        def respond(request):
            return [ack(request), update(trade("BTC_USDT"))] if request["id"] == 1 else [ack(request)]

        websocket = FakeWebSocket(respond)
        feed = feed_for(websocket)
        await feed.set_symbols(["BTC_USDT", "ETH_USDT"])
        stream = feed.stream()
        await anext(stream)
        await feed.set_symbols(["ETH_USDT", "SOL_USDT"])
        await stream.aclose()
        # 断开后的变更只记录目标集合，不应再向旧连接发送。
        await feed.set_symbols(["XRP_USDT"])
        return websocket, feed

    websocket, feed = asyncio.run(scenario())

    assert [(request["event"], request["payload"]) for request in websocket.sent] == [
        ("subscribe", ["BTC_USDT", "ETH_USDT"]),
        ("unsubscribe", ["BTC_USDT"]),
        ("subscribe", ["SOL_USDT"]),
    ]
    assert feed.symbols == ["XRP_USDT"]


def test_receive_timeout_is_reported_as_connection_error():
    async def scenario():
        feed = feed_for(FakeWebSocket(), receive_timeout_seconds=0.01)
        await feed.set_symbols(["BTC_USDT"])
        stream = feed.stream()
        try:
            await anext(stream)
        finally:
            await stream.aclose()

    with pytest.raises(ConnectionError, match="未收到 Gate 行情"):
        asyncio.run(scenario())
