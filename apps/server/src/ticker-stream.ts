import ccxt from 'ccxt'

type GateSocket = InstanceType<typeof ccxt.pro.gate>
export type LivePrice = { price: number; receivedAt: number }

export class GateTickerStream {
  readonly prices = new Map<string, LivePrice>()
  private socket: GateSocket | null = null
  private generation = 0
  private stopped = false

  constructor(private readonly markets: InstanceType<typeof ccxt.gate>) {}

  async replace(symbols: string[]): Promise<void> {
    // 代次用于丢弃旧连接关闭过程中迟到的消息，避免已剔除交易对重新写入价格缓存。
    this.generation++
    const old = this.socket
    this.socket = null
    if (old) {
      try {
        await old.close()
      } catch (error) {
        console.error('[旧行情连接关闭失败]', error)
      }
    }
    if (symbols.length === 0 || this.stopped) return

    // Gate 的 CCXT 实现不支持退订；名单改变时重建连接，避免继续接收被剔除的交易对。
    const socket = new ccxt.pro.gate({ enableRateLimit: true, options: { defaultType: 'swap' }, newUpdates: true })
    socket.setMarketsFromExchange(this.markets)
    this.socket = socket
    const generation = this.generation
    // 分批发送订阅，避免一个过长 payload 给 Gate WebSocket 带来突发负载。
    for (let offset = 0; offset < symbols.length; offset += 50) {
      void this.watchChunk(socket, symbols.slice(offset, offset + 50), generation)
    }
  }

  async stop(): Promise<void> {
    this.stopped = true
    this.generation++
    await this.socket?.close()
    this.socket = null
  }

  private async watchChunk(socket: GateSocket, symbols: string[], generation: number): Promise<void> {
    while (!this.stopped && generation === this.generation) {
      try {
        const updates = await socket.watchTickers(symbols)
        if (generation !== this.generation) return
        const now = Date.now()
        for (const [symbol, ticker] of Object.entries(updates)) {
          const price = ticker.last
          if (symbols.includes(symbol) && typeof price === 'number' && Number.isFinite(price) && price > 0) {
            this.prices.set(symbol, { price, receivedAt: now })
          }
        }
      } catch (error) {
        if (this.stopped || generation !== this.generation) return
        console.error(`[行情连接异常] ${symbols[0]} 等 ${symbols.length} 个交易对，5 秒后重试`, error)
        await new Promise(resolve => setTimeout(resolve, 5_000))
      }
    }
  }
}
