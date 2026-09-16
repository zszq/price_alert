"""价格异动监控的领域模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    quote_volume: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp 必须包含时区")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("K 线价格必须大于 0")
        if self.high < self.low:
            raise ValueError("K 线最高价不能低于最低价")


@dataclass(frozen=True, slots=True)
class PriceTick:
    symbol: str
    price: float
    size: float
    timestamp: datetime
    trade_id: str | None = None

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("price 必须大于 0")
        if self.size < 0:
            raise ValueError("size 不能小于 0")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp 必须包含时区")


@dataclass(frozen=True, slots=True)
class ContractTicker:
    symbol: str
    last_price: float
    volume_24h_quote: float


@dataclass(frozen=True, slots=True)
class PriceAlert:
    symbol: str
    direction: Literal["surge", "drop"]
    price: float
    reference_price: float
    change_percent: float
    move_atr: float
    atr: float
    atr_period: int
    lookback_seconds: int
    trade_count: int
    volume_24h_quote: float
    timestamp: datetime

    @property
    def color(self) -> Literal["green", "red"]:
        return "green" if self.direction == "surge" else "red"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.astimezone(UTC).isoformat()
        # 结构化通知也携带展示语义，Webhook 接收方无需重复判断方向。
        payload["color"] = self.color
        return payload
