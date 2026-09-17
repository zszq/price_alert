# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Gate.io 虚拟币 USDT 永续合约的实时价格异动监控服务（Python 3.11+）。只监控和提醒，不交易。用户可见文本、日志、注释和 README 均为简体中文。

## 常用命令

所有命令都应在仓库根目录执行：`config/default.yaml`、`data/price-alert.lock`、`data/alerts/alerts.jsonl` 都是相对当前工作目录解析的。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

.\.venv\Scripts\python.exe -m pytest                                   # 全部测试（离线）
.\.venv\Scripts\python.exe -m pytest tests/test_detector.py            # 单个文件
.\.venv\Scripts\python.exe -m pytest tests/test_detector.py -k cooldown # 单个用例
.\.venv\Scripts\python.exe -m pytest --cov=price_alert                 # 覆盖率
.\.venv\Scripts\ruff.exe check src tests                               # lint（E/F/I/UP/B，行宽 120）

.\.venv\Scripts\python.exe -m price_alert.cli check-config  # 校验配置
.\.venv\Scripts\python.exe -m price_alert.cli simulate      # 合成行情跑一遍检测，不联网、不写文件
.\.venv\Scripts\python.exe -m price_alert.cli universe      # 联网列出当前合约池
.\.venv\Scripts\python.exe -m price_alert.cli run           # 实时监控（或双击 start-monitor.bat）
```

`run` 持有 `data/price-alert.lock` 进程锁，已有实例运行时再次启动会直接退出。

## 架构

数据流：`universe.py` 筛选合约池 → `gate.py` REST 拉 K 线预热 ATR → `gate.py` WebSocket `futures.trades` 推送成交 → `detector.py` 判定 → `notifier.py` 分发（控制台 / JSONL / Webhook）。`service.py::run_monitor` 负责把这些串起来。

### 服务主循环（service.py）

- 合约池刷新与 WebSocket 会话是绑定的：每个 WS 会话被 `asyncio.timeout(距下次刷新的秒数)` 包住，超时即断开、重新筛选合约、按新合约列表重新连接订阅。因此 `except TimeoutError` 表示“到达刷新周期”而不是故障。
- 为了不与上面混淆，`GateTradeFeed` 内部的接收超时被转换成 `ConnectionError`，走指数退避重连分支。修改超时处理时要保持这种区分。
- `_sync_universe` 做增量同步：移除的合约丢弃状态（含冷却记录），保留的合约只更新成交额，新增合约并发（`warmup_concurrency`）拉 K 线预热，且会剔除尚未收盘的当前 K 线。预热失败的合约仍加入，靠实时 K 线自行就绪。
- `GateRestClient` 是同步 `urllib` 实现，异步代码中通过 `asyncio.to_thread` 调用。

### 检测器（detector.py）

每个合约一个 `_SymbolState`，核心语义：

- 成交按秒聚合为 `_SecondBucket`（VWAP，零成交量时退化为算术均价）。**只有在下一秒的第一笔成交到达时才会评估刚结束的那一秒**，所以没有新成交就不会产生判定；测试和 `simulate` 都要多送一秒的成交来“结算”最后一个桶。
- 乱序（时间早于上一笔）的成交直接丢弃。
- ATR 由 `indicators.WilderAtr` 计算：预热 K 线 seed 后，实时成交维护 `_LiveBar`，跨入下一个 K 线周期时才把上一根 bar 喂给 ATR。ATR 超过 `max_atr_age_seconds` 未更新则不判定。
- 触发条件（全部满足）：基准桶在 `lookback_seconds` 前且间隔不超过 lookback+2 秒；窗口内成交笔数 ≥ `min_window_trades`；`|涨跌幅| ≥ min_change_percent` **且** `位移/ATR ≥ trigger_atr_multiple`；同方向连续 `confirmation_seconds` 个相邻秒满足（中间缺一秒或任一条件不满足就重置候选）；同一合约不分方向的冷却期已过。

### 配置（config.py + config/default.yaml）

- pydantic 模型全部 `extra="forbid"`，YAML 中出现未知键会直接报错。
- `PRICE_ALERT_WEBHOOK_URL` 环境变量覆盖 `alerts.webhook_url`。
- 代码默认值与 `config/default.yaml` 必须保持一致，并由配置测试校验；实际运行仍以 YAML 为准。

### 新增/修改检测参数时需要同步的位置

`AtrMoveDetector` 的构造参数在两处被手工组装：`service.py::run_monitor` 和 `cli.py::simulate`。新增指标参数需要同时改 `config.py` 模型、`config/default.yaml`、这两处构造、README 的配置说明。K 线周期到秒数的映射在 `service.INTERVAL_SECONDS` 和 `IndicatorConfig` 校验器中各有一份。

### 合约池筛选（universe.py）

只接受 `contract_type == ""`（字段必须存在且为空，非币类资产会有分类值）、`status == "trading"`、以 `_USDT` 结尾、`volume_24h_quote` **严格大于**门槛的合约。`volume_24h_usd` 已被 Gate 弃用，仅作缺字段时的回退。`is_internal=true` 的成交在 `gate.parse_trade_payload` 中被忽略。

### 通知（notifier.py）

`NotifierHub` 用 `gather(return_exceptions=True)` 并发发送，单个通道失败只记日志不影响监控。提醒时间统一转为北京时间；控制台暴涨绿色、暴跌红色；JSONL/Webhook 保留完整结构化字段（`PriceAlert.to_dict()`）。修改提醒文本格式时注意 README 中的示例。

## 约定

- 测试必须确定、离线；文件名 `tests/test_<module>.py`，覆盖边界条件、异常交易所数据、冷却、通知失败等场景。
- 修改 `config/default.yaml` 中的阈值属于行为变更，提交说明中需写明运营影响。
- 提交信息风格：简洁祈使句，常用 `config:`、`docs:`、`feat(...)`、`chore:` 前缀，也可用中文标题。
