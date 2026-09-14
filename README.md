# Kinetic Energy

基于 pnpm workspace 的 TypeScript 项目，包含 Node.js 服务端和 React Web 端。

## 环境要求

- Node.js 22.12 或更新版本
- pnpm 11.4

## 启动

```bash
pnpm install
pnpm dev
```

- Web 端：http://localhost:5173
- 服务端健康检查：http://localhost:3000/api/health

服务端启动后会自动监控 Gate.io 活跃的 USDT 本位永续合约。筛选口径为最近 144 根已完成的 5 分钟 K 线的报价币种成交额合计不少于 1,000 万 USDT；每个 UTC 5 分钟边界后刷新名单。报警在服务端控制台输出，交互终端中上涨为绿色、下跌为红色，并通过终端响铃（Windows 另尝试系统蜂鸣）提示。

可选环境变量：`MIN_12H_TURNOVER_USDT`（默认 10000000）、`ALERT_1M_PERCENT`（默认 1）、`ALERT_5M_PERCENT`（默认 2）、`ALERT_SIGMA`（默认 3）、`ALERT_COOLDOWN_SECONDS`（默认 300）、`GATE_FUTURES_API_URL`（默认 Gate 官方合约备用域名 `https://fx-api.gateio.ws/api/v4`）。异动阈值取绝对涨跌幅下限与最近 10 分钟 5 秒收益率标准差换算值的较大者；首次至少积累 1 分钟价格样本。控制台和 `/api/health` 的时间统一显示为北京时间 `YYYY-MM-DD HH:mm:ss`；健康接口的 `monitor` 字段还提供监听数量和错误状态。公开行情无需 API Key。

开发时，Vite 会把 `/api` 请求代理到服务端，因此 Web 端可以使用相对路径访问 API。部署时需由反向代理或同域服务提供对应的 `/api` 路径。

## 常用命令

```bash
pnpm build          # 构建两个应用
pnpm typecheck      # 检查两个应用的 TypeScript 类型
pnpm format         # 格式化整个工作区
pnpm format:check   # 检查格式
pnpm --filter @kinetic-energy/server test  # 服务端关键逻辑测试
```

VS Code 安装工作区推荐的 Prettier 扩展后，保存文件会自动格式化。其他编辑器可启用 Prettier 的保存时格式化功能。
