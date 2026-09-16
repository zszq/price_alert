# Gate.io ATR Price Alert

一个只面向 Gate.io 虚拟币 USDT 永续合约的实时价格异动监控服务。股票、指数、外汇、贵金属和商品合约不会进入监控。项目只负责监控和提醒，不执行交易。

## 检测逻辑

项目不使用固定涨跌幅作为触发门槛，而是使用 ATR 对短时价格位移进行标准化：

```text
短时异动强度 = |当前成交价 - N 秒前价格| / Wilder ATR
```

默认配置为：

- 从 Gate.io 全部虚拟币 USDT 永续合约中，自动选择 24 小时计价成交额严格大于 10,000,000 USDT 的合约；
- 使用最近 14 根 1 分钟 K 线计算 Wilder ATR；
- 比较当前成交价与 30 秒前价格；
- 位移达到 0.8 ATR 且窗口内至少 5 笔成交时提醒；
- 同一合约、同一涨跌方向在 120 秒内只提醒一次；
- 每 10 分钟刷新交易对池，新增和移除合约自动生效。

百分比涨跌只作为提醒中的辅助信息展示，不参与触发判断。ATR 会随着近期波动率自动变化：平静市场使用更小的价格尺度，高波动市场自动提高门槛。

## 数据流程

```text
Gate REST /futures/usdt/contracts + /tickers
        ↓ 仅虚拟币、交易中、24h quote volume > 10M USDT
动态交易对池
        ↓
Gate REST candlesticks 预热 ATR
        ↓
Gate WebSocket futures.trades 实时成交
        ↓
按秒价格窗口 + 实时分钟 K 线 + Wilder ATR
        ↓
完整窗口 / 成交笔数 / ATR 新鲜度 / 冷却过滤
        ↓
控制台声音 + JSONL + 可选 Webhook
```

项目通过 Gate 合约元数据的 `contract_type` 排除股票、指数、外汇、贵金属和商品等非币类合约，并且只监控 `status=trading` 的品种。Gate 官方将 `volume_24h_usd` 标记为弃用，因此项目使用 `volume_24h_quote`。`is_internal=true` 的内部成交可能偏离正常盘口且不会进入 K 线，检测时会主动忽略。

## 安装

需要 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 启动

Windows 可以双击 `start-monitor.bat`，或者运行：

```powershell
.\.venv\Scripts\python.exe -m price_alert.cli run
```

停止监控请按 `Ctrl+C`。

查看当前满足成交额条件的合约：

```powershell
.\.venv\Scripts\python.exe -m price_alert.cli universe
```

校验配置：

```powershell
.\.venv\Scripts\python.exe -m price_alert.cli check-config
```

使用合成数据验证 ATR 提醒，不联网、不写告警文件：

```powershell
.\.venv\Scripts\python.exe -m price_alert.cli simulate
```

## 提醒示例

```text
[暴涨提醒] BTC_USDT 在 30 秒内移动 0.92 ATR（+0.35%） | 75800 → 76065 | ATR(14)=288.2 | 24h成交额 7011.4M USDT
```

控制台中暴涨提醒显示为绿色，暴跌提醒显示为红色。JSONL 和 Webhook 记录也包含 `color` 字段，值为 `green` 或 `red`。

实时提醒默认追加到 `data/alerts/alerts.jsonl`。每行是一条完整 JSON，即使程序异常退出，也不会破坏之前的记录。

## Webhook

可以在 `config/default.yaml` 填写通用 HTTP Webhook，也可以用环境变量覆盖：

```powershell
$env:PRICE_ALERT_WEBHOOK_URL = "https://example.com/your-webhook"
.\.venv\Scripts\python.exe -m price_alert.cli run
```

环境变量优先，适合包含密钥的地址。Webhook 会收到可直接展示的 `text`，以及 ATR、ATR 倍数、起止价格、成交额和时间等结构化字段。单个通知通道失败不会中断行情监控。

## 配置说明

`config/default.yaml` 的主要参数：

- `gate.min_volume_24h_quote`：24 小时 USDT 计价成交额门槛；
- `gate.universe_refresh_seconds`：交易对池刷新周期；
- `indicator.candle_interval`：ATR K 线周期；
- `indicator.atr_period`：Wilder ATR 周期；
- `indicator.lookback_seconds`：短时位移观察窗口；
- `indicator.trigger_atr_multiple`：触发所需 ATR 倍数；
- `indicator.min_window_trades`：窗口内最低成交笔数；
- `indicator.max_atr_age_seconds`：ATR 过期保护；
- `alerts.cooldown_seconds`：同方向提醒冷却时间；
- `alerts.console_colors`：是否启用控制台颜色，默认开启；

## 工程结构

```text
config/default.yaml          Gate、ATR、提醒配置
src/price_alert/
├── config.py                配置模型与严格校验
├── models.py                领域模型
├── universe.py              动态高成交额合约池
├── gate.py                  Gate REST / WebSocket 适配器
├── indicators.py            Wilder ATR
├── detector.py              ATR 标准化异动检测
├── notifier.py              控制台、JSONL、Webhook
├── service.py               预热、刷新、重连和服务编排
└── cli.py                   run/universe/check-config/simulate
tests/                       指标、筛选、解析、检测和通知测试
data/alerts/                 本地告警记录
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check src tests
```

该工具只提供行情提醒，不构成投资建议。
