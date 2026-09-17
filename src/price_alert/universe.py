"""基于 Gate.io 24 小时计价成交额构建动态合约池。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from price_alert.models import ContractTicker


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def select_liquid_contracts(
    tickers: Iterable[Mapping[str, Any]],
    contracts: Iterable[Mapping[str, Any]],
    min_volume_24h_quote: float,
    retained_symbols: Iterable[str] = (),
    exit_volume_ratio: float = 1.0,
) -> list[ContractTicker]:
    crypto_symbols = {
        str(contract.get("name", "")).upper()
        for contract in contracts
        # Gate 会给股票、指数、贵金属等传统资产填写分类；只接受未分类的币本位标的，
        # 严格要求字段存在且为空，避免接口结构变化时误收非币类资产。
        if contract.get("contract_type") == ""
        and str(contract.get("status", "")).lower() == "trading"
        # 下架流程中的合约仍可能显示 trading，但流动性会迅速枯竭，继续监控只会制造噪声。
        and not contract.get("in_delisting")
    }
    retained = {symbol.upper() for symbol in retained_symbols}
    # 已在监控中的合约用更低的退出门槛，避免成交额在门槛附近波动时被反复移除、重新预热。
    exit_volume = min_volume_24h_quote * exit_volume_ratio
    selected: list[ContractTicker] = []
    for ticker in tickers:
        symbol = str(ticker.get("contract", "")).upper()
        last_price = _number(ticker.get("last"))
        # Gate 已弃用 volume_24h_usd；旧响应只在缺少新字段时作为兼容回退。
        volume = _number(ticker.get("volume_24h_quote", ticker.get("volume_24h_usd")))
        threshold = exit_volume if symbol in retained else min_volume_24h_quote
        if symbol in crypto_symbols and symbol.endswith("_USDT") and last_price > 0 and volume > threshold:
            selected.append(ContractTicker(symbol, last_price, volume))
    return sorted(selected, key=lambda item: item.volume_24h_quote, reverse=True)
