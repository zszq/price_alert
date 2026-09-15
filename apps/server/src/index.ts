import express from 'express'
import { GateMonitor } from './monitor.js'

const app = express()
const port = Number(process.env.PORT ?? 3000)
const monitor = new GateMonitor()

app.get('/api/health', (_request, response) => {
  response.json({ status: 'ok', monitor: monitor.status() })
})

const server = app.listen(port, () => {
  console.log(`服务端已启动：http://localhost:${port}`)
  monitor.start()
})

// 先停止接收新请求，再释放行情连接；无论清理是否报错都要响应系统退出信号。
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.once(signal, () => {
    server.close()
    void monitor.stop().finally(() => process.exit(0))
  })
}
