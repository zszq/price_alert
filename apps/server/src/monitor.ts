import { announce } from './alert.js'
import { config } from './config.js'
import { PriceDetector } from './detector.js'
import { CANDLE_SECONDS, createGateMarketClient, findQualifiedMarkets, type QualifiedMarket } from './market.js'
import { GateTickerStream } from './ticker-stream.js'

export class GateMonitor {
  private readonly rest = createGateMarketClient()
  private readonly detector = new PriceDetector()
  private readonly tickerStream = new GateTickerStream(this.rest)
  private qualified = new Map<string, QualifiedMarket>()
  private refreshTimer: NodeJS.Timeout | null = null
  private sampleTimer: NodeJS.Timeout | null = null
  private stopped = false
  private marketsLoadedAt = 0
  private refreshedAt: string | null = null
  private lastError: string | null = null

  status() {
    return {
      state: this.stopped ? 'stopped' : this.lastError ? 'degraded' : this.refreshedAt ? 'active' : 'initializing',
      monitoredCount: this.qualified.size,
      refreshedAt: this.refreshedAt,
      lastError: this.lastError,
    }
  }

  start(): void {
    this.sampleTimer = setInterval(() => this.samplePrices(), config.sampleMs)
    void this.refresh()
  }

  async stop(): Promise<void> {
    this.stopped = true
    if (this.refreshTimer) clearTimeout(this.refreshTimer)
    if (this.sampleTimer) clearInterval(this.sampleTimer)
    await Promise.allSettled([this.tickerStream.stop(), this.rest.close()])
  }

  private async refresh(): Promise<void> {
    if (this.stopped) return
    try {
      if (!this.marketsLoadedAt || Date.now() - this.marketsLoadedAt >= 60 * 60_000) {
        await this.rest.loadMarkets(true)
        this.marketsLoadedAt = Date.now()
      }
      // 与交易所 5 分钟 K 线边界对齐，才可稳定比较相同长度的 12 小时窗口。
      const boundary = Math.floor(Date.now() / (CANDLE_SECONDS * 1_000)) * CANDLE_SECONDS
      const next = await findQualifiedMarkets(this.rest, config.minTurnover, boundary, this.qualified)
      if (this.stopped) return
      await this.applyQualified(next)
      this.refreshedAt = new Date().toISOString()
      this.lastError = null
      console.info(`[交易对刷新] ${this.refreshedAt} 符合条件 ${next.size} 个`)
      this.scheduleRefresh(false)
    } catch (error) {
      this.lastError = error instanceof Error ? error.message : String(error)
      console.error('[交易对刷新失败] 保留上一轮名单，30 秒后重试', error)
      this.scheduleRefresh(true)
    }
  }

  private scheduleRefresh(retry: boolean): void {
    if (this.stopped) return
    const interval = CANDLE_SECONDS * 1_000
    const now = Date.now()
    // 边界后留三秒给交易所完成上一根 K 线；失败则较快重试而不等下个周期。
    const nextBoundary = (Math.floor(now / interval) + 1) * interval + 3_000
    this.refreshTimer = setTimeout(() => void this.refresh(), retry ? 30_000 : nextBoundary - now)
  }

  private async applyQualified(next: Map<string, QualifiedMarket>): Promise<void> {
    const before = [...this.qualified.keys()].sort()
    const after = [...next.keys()].sort()
    const changed = before.length !== after.length || before.some((symbol, index) => symbol !== after[index])

    // 先成功切换连接再提交名单；切换失败时下轮仍会重试，避免有名单却无订阅。
    if (changed) await this.tickerStream.replace(after)
    for (const symbol of before) {
      if (!next.has(symbol)) {
        this.detector.remove(symbol)
        this.tickerStream.prices.delete(symbol)
        console.info(`[取消监听] ${symbol}`)
      }
    }
    this.qualified = next
    if (changed) {
      for (const symbol of after) if (!before.includes(symbol)) console.info(`[开始监听] ${symbol}`)
    }
  }

  private samplePrices(): void {
    const now = Date.now()
    for (const [symbol, market] of this.qualified) {
      const live = this.tickerStream.prices.get(symbol)
      if (!live || now - live.receivedAt > 30_000) continue
      const alert = this.detector.sample(symbol, live.price, market.turnover, now)
      if (alert) announce(alert)
    }
  }
}
