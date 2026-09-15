import ccxt from 'ccxt'

export const CANDLE_SECONDS = 300
export const CANDLE_COUNT = 144

type GateTicker = { contract?: string; volume_24h_quote?: string }
type GateCandle = { t?: number; sum?: string }
export type QualifiedMarket = { symbol: string; turnover: number }

/** 只加载目标市场，避免 CCXT 默认抓取现货、期权和币种元数据引入额外故障点。 */
export function createGateMarketClient(): InstanceType<typeof ccxt.gate> {
  const exchange = new ccxt.gate({
    enableRateLimit: true,
    urls: { api: { public: { futures: process.env.GATE_FUTURES_API_URL ?? 'https://fx-api.gateio.ws/api/v4' } } },
    options: {
      defaultType: 'swap',
      fetchMarkets: { types: ['swap'] },
      swap: { fetchMarkets: { settlementCurrencies: ['usdt'] } },
    },
  })
  // 只监控 USDT 永续，加载现货币种和其他市场会增加无关请求与故障点。
  exchange.has['fetchCurrencies'] = false
  return exchange
}

/** 使用已完成的 K 线，避免正在形成的蜡烛使 12 小时成交额随刷新时刻抖动。 */
export function sumTwelveHours(candles: GateCandle[], boundarySeconds: number): number | null {
  const start = boundarySeconds - CANDLE_COUNT * CANDLE_SECONDS
  const seen = new Set<number>()
  let turnover = 0

  for (const candle of candles) {
    const timestamp = Number(candle.t)
    const amount = Number(candle.sum)
    if (!Number.isInteger(timestamp) || timestamp < start || timestamp >= boundarySeconds) continue
    if (timestamp % CANDLE_SECONDS !== 0 || seen.has(timestamp)) return null
    if (!Number.isFinite(amount) || amount < 0) return null
    seen.add(timestamp)
    turnover += amount
  }

  // Gate 可能省略零成交 K 线；只接受至少一根有效 K 线，并拒绝超出窗口的数据。
  return seen.size > 0 ? turnover : null
}

/** 并发核验候选合约最近 12 小时的成交额，并在单品种请求失败时保留旧结果。 */
export async function findQualifiedMarkets(
  exchange: InstanceType<typeof ccxt.gate>,
  minTurnover: number,
  boundarySeconds: number,
  previous: Map<string, QualifiedMarket>,
): Promise<Map<string, QualifiedMarket>> {
  const markets = Object.values(exchange.markets ?? {}).filter(market => market.active !== false && market.swap && market.linear && market.settle === 'USDT')
  const byId = new Map(markets.map(market => [market.id, market]))
  const raw = (await exchange.publicFuturesGetSettleTickers({ settle: 'usdt' })) as GateTicker[]
  if (!Array.isArray(raw) || raw.length === 0) throw new Error('Gate 合约 ticker 为空')

  // 24 小时成交额必然不小于其内部的 12 小时成交额，用它预筛可减少逐对 K 线请求。
  const candidates = raw.filter(ticker => {
    if (!ticker.contract || !byId.has(ticker.contract)) return false
    const volume = Number(ticker.volume_24h_quote)
    // 缺少 24 小时字段时仍查 K 线，避免预筛误删。
    return !Number.isFinite(volume) || volume >= minTurnover
  })
  const result = new Map<string, QualifiedMarket>()
  const concurrency = 4
  // JavaScript 在两个 await 之间同步执行，因此多个 worker 可安全领取不同的数组下标。
  let cursor = 0

  await Promise.all(
    Array.from({ length: Math.min(concurrency, candidates.length) }, async () => {
      while (cursor < candidates.length) {
        const ticker = candidates[cursor++]
        const market = byId.get(ticker.contract!)!
        try {
          const candles = (await exchange.publicFuturesGetSettleCandlesticks({
            settle: 'usdt',
            contract: market.id,
            interval: '5m',
            from: boundarySeconds - CANDLE_COUNT * CANDLE_SECONDS,
            to: boundarySeconds - 1,
          })) as GateCandle[]
          const turnover = sumTwelveHours(candles, boundarySeconds)
          if (turnover === null) throw new Error('12 小时 K 线缺失或无效')
          if (turnover >= minTurnover) result.set(market.symbol, { symbol: market.symbol, turnover })
        } catch (error) {
          console.error(`[筛选失败] ${market.symbol}`, error)
          // 单个品种临时失败不能等同于成交额跌破门槛，否则会误退订正在监控的品种。
          const old = previous.get(market.symbol)
          if (old) result.set(market.symbol, old)
        }
      }
    }),
  )

  return result
}
