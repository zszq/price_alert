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
