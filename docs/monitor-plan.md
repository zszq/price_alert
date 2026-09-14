# Gate.io 合约价格异动监控方案与任务

## 调研结论

- 监控范围定义为活跃的 USDT 本位永续合约；成交额单位为 USDT。Gate 的合约 ticker 只有近 24 小时 `volume_24h_quote`，可用于预筛，但不能代替 12 小时统计。[Gate API](https://www.gate.com/docs/developers/apiv4/en/)
- Gate 合约 K 线的 `sum` 是报价币种成交额。使用 CCXT 提供的 Gate 原生公开接口读取该字段，汇总最近 144 根已完成的 5 分钟 K 线，得到截至最近 5 分钟边界的 12 小时成交额。[Gate 官方 SDK 字段说明](https://github.com/gateio/gateapi-java/blob/master/docs/FuturesCandlestick.md)
- Gate 官方提供合约专用 REST 备用域名 `https://fx-api.gateio.ws/api/v4`，默认使用它，并允许通过环境变量覆盖。[Gate API 接入地址](https://www.gate.com/docs/developers/apiv4/en/)
- CCXT 的 `fetchTickers` 调用成本较高，不宜高频轮询；其 WebSocket `watchTicker` / `watchTickers` 用于连续行情监听。已检查本项目安装的 CCXT 4.5.78：Gate 的 `unWatchTicker` / `unWatchTickers` 均不支持，因此名单变化时需重建行情连接，才能真正退订。[CCXT REST 手册](https://github.com/ccxt/ccxt/wiki/manual)、[CCXT WebSocket 手册](https://github.com/ccxt/ccxt/wiki/ccxt.pro.manual)
- 常见的异动判定会同时考虑短时收益率和近期波动基线。这里以滚动收益率标准差做动态门槛，并用绝对涨跌幅下限控制低波动品种的噪声；阈值属于默认监控参数，可在部署时调整。[波动率估计研究](https://arxiv.org/abs/2105.14382)

## 实施方案

1. 启动时加载市场；只选 `active`、`swap`、`linear`、`settle=USDT` 的交易对。先按 24 小时报价成交额预筛，再逐一读取最近 12 小时的 5 分钟 K 线 `sum`，仅保留成交额不少于 10,000,000 USDT 的交易对。数据不完整或接口失败时，不以错误结果覆盖上一轮有效名单。
2. 启动后立即筛选，之后在每个 UTC 5 分钟边界后重新筛选；名单变化时重建 WebSocket 行情连接以退订不合格交易对，并清除其价格历史与报警状态。网络错误重试并记录日志，避免单个交易对失败拖垮整个服务。
3. 对每个在名单中的交易对监听最新成交价。将行情压缩为固定时间间隔样本，计算 1 分钟和 5 分钟对数收益率；从更早的样本构建滚动波动基线，判定同时满足绝对涨跌幅门槛与动态标准差门槛的上冲/下跌。只用过去数据作基线，过滤无效价格、重复时间戳和过旧数据，并对同一交易对同方向设报警冷却。
4. 报警输出时间、交易对、方向、价格、窗口涨跌幅、动态阈值和 12 小时成交额；向运行服务的终端写入响铃字符。终端是否实际发声取决于终端/系统设置。
5. 配置通过环境变量提供；健康检查保留，加入监控状态供运行时查看。停止服务时清理定时器、订阅和连接。

## 任务

- [x] 添加 CCXT 依赖并验证 Gate 市场、K 线和 WebSocket 能力及字段。
- [x] 实现 12 小时成交额筛选、周期刷新和名单同步。
- [x] 实现行情监听、异动检测、报警与提示音。
- [x] 补充关键逻辑测试、配置与运行文档，运行类型检查和构建。
