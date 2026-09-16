import asyncio
import json
from datetime import UTC, datetime

from colorama import Fore, Style

from price_alert.models import PriceAlert
from price_alert.notifier import JsonlNotifier, colorize_alert, format_alert


def test_jsonl_notifier_writes_atr_alert(tmp_path):
    alert = PriceAlert(
        symbol="BTC_USDT",
        direction="surge",
        price=101.0,
        reference_price=100.0,
        change_percent=1.0,
        move_atr=1.25,
        atr=0.8,
        atr_period=14,
        lookback_seconds=30,
        trade_count=10,
        volume_24h_quote=1_000_000_000,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    target = tmp_path / "alerts.jsonl"

    asyncio.run(JsonlNotifier(target).send(alert))

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["move_atr"] == 1.25
    assert payload["color"] == "green"
    assert "1.25 ATR" in format_alert(alert)
    assert colorize_alert(alert, "surge") == f"{Fore.GREEN}surge{Style.RESET_ALL}"


def test_drop_alert_is_red():
    alert = PriceAlert(
        symbol="ETH_USDT",
        direction="drop",
        price=99.0,
        reference_price=100.0,
        change_percent=-1.0,
        move_atr=1.25,
        atr=0.8,
        atr_period=14,
        lookback_seconds=30,
        trade_count=10,
        volume_24h_quote=1_000_000_000,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert alert.to_dict()["color"] == "red"
    assert colorize_alert(alert, "drop") == f"{Fore.RED}drop{Style.RESET_ALL}"
