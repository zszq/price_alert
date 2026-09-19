import asyncio

import pytest

from price_alert import cli
from price_alert.config import AppConfig


@pytest.mark.parametrize(
    "indicator",
    [
        {},
        {"lookback_seconds": 60},
        {"lookback_seconds": 5, "confirmation_seconds": 5, "min_window_trades": 100},
        {"candle_interval": "5m", "atr_period": 2, "warmup_candles": 15, "trigger_atr_multiple": 20},
        {"lookback_seconds": 3600, "max_atr_age_seconds": 120},
    ],
)
def test_simulate_produces_one_alert_for_valid_configurations(indicator, capsys):
    config = AppConfig.model_validate({"indicator": indicator, "alerts": {"console_colors": False}})

    assert asyncio.run(cli.simulate(config)) == 1
    assert "暴涨提醒" in capsys.readouterr().out


def test_missing_config_file_exits_with_readable_message(tmp_path):
    with pytest.raises(SystemExit, match="配置文件不存在"):
        cli._load_config_or_exit(str(tmp_path / "missing.yaml"))


def test_invalid_config_lists_field_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("indicator:\n  atr_period: 1\n", encoding="utf-8")

    with pytest.raises(SystemExit, match=r"indicator\.atr_period"):
        cli._load_config_or_exit(str(path))
