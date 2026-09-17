from pathlib import Path

import pytest
from pydantic import ValidationError

from price_alert.config import AppConfig, load_config


def test_load_config_and_webhook_environment_override(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
gate:
  min_volume_24h_quote: 10000001
indicator:
  trigger_atr_multiple: 1.25
alerts:
  webhook_url: null
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRICE_ALERT_WEBHOOK_URL", "https://example.test/hook")

    config = load_config(path)

    assert config.gate.min_volume_24h_quote == 10_000_001
    assert config.indicator.trigger_atr_multiple == 1.25
    assert config.alerts.webhook_url == "https://example.test/hook"


def test_rejects_atr_configuration_without_enough_warmup_data():
    with pytest.raises(ValidationError, match="warmup_candles"):
        AppConfig.model_validate({"indicator": {"atr_period": 20, "warmup_candles": 20}})


def test_noise_protection_defaults_are_enabled():
    config = AppConfig()

    assert config.indicator.trigger_atr_multiple == 1.5
    assert config.indicator.min_change_percent == 1.0
    assert config.indicator.confirmation_seconds == 3


def test_code_defaults_match_default_yaml(monkeypatch):
    # 清除外部覆盖，确保比较的是两套默认配置本身，而不是运行环境。
    monkeypatch.delenv("PRICE_ALERT_WEBHOOK_URL", raising=False)
    default_yaml = Path(__file__).resolve().parents[1] / "config" / "default.yaml"

    assert AppConfig() == load_config(default_yaml)


def test_rejects_reconnect_max_below_initial_delay():
    with pytest.raises(ValidationError, match="reconnect_max_seconds"):
        AppConfig.model_validate({"gate": {"reconnect_initial_seconds": 10, "reconnect_max_seconds": 5}})


def test_rejects_confirmation_longer_than_lookback():
    with pytest.raises(ValidationError, match="confirmation_seconds"):
        AppConfig.model_validate({"indicator": {"lookback_seconds": 5, "confirmation_seconds": 6}})


def test_atr_age_default_follows_candle_interval_but_explicit_value_is_validated():
    assert AppConfig.model_validate({"indicator": {"candle_interval": "5m"}}).indicator.max_atr_age_seconds == 900

    with pytest.raises(ValidationError, match="max_atr_age_seconds"):
        AppConfig.model_validate({"indicator": {"candle_interval": "5m", "max_atr_age_seconds": 180}})
