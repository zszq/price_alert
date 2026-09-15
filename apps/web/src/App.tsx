import { useEffect, useState } from 'react'

type Health = { status: string }

/** 展示最小可用的前端状态，并在挂载时验证反向代理到服务端的链路。 */
export default function App() {
  const [message, setMessage] = useState('正在连接服务端…')

  useEffect(() => {
    const controller = new AbortController()

    fetch('/api/health', { signal: controller.signal })
      .then(response => {
        if (!response.ok) throw new Error('服务端响应异常')
        return response.json() as Promise<Health>
      })
      .then(data => setMessage(data.status === 'ok' ? '服务端连接正常' : '服务端响应异常'))
      .catch((error: unknown) => {
        if (error instanceof Error && error.name === 'AbortError') return
        setMessage('无法连接服务端')
      })

    // 卸载时取消请求，避免开发模式重复挂载产生过期状态更新。
    return () => controller.abort()
  }, [])

  return (
    <main className="page">
      <div className="card">
        <p className="eyebrow">Kinetic Energy</p>
        <h1>项目已就绪</h1>
        <p>React Web 端与 Node.js 服务端已接入同一个 pnpm 工作区。</p>
        <div className="status" role="status">
          <span className="status-dot" />
          {message}
        </div>
      </div>
    </main>
  )
}
