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

开发时，Vite 会把 `/api` 请求代理到服务端，因此 Web 端可以使用相对路径访问 API。部署时需由反向代理或同域服务提供对应的 `/api` 路径。

## 常用命令

```bash
pnpm build          # 构建两个应用
pnpm typecheck      # 检查两个应用的 TypeScript 类型
pnpm format         # 格式化整个工作区
pnpm format:check   # 检查格式
```

VS Code 安装工作区推荐的 Prettier 扩展后，保存文件会自动格式化。其他编辑器可启用 Prettier 的保存时格式化功能。
