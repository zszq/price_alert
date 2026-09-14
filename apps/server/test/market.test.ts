import assert from 'node:assert/strict'
import { test } from 'node:test'
import ccxt from 'ccxt'
import { CANDLE_COUNT, CANDLE_SECONDS, findQualifiedMarkets, sumTwelveHours } from '../src/market.js'

test('只汇总目标 12 小时内的报价成交额', () => {
  const boundary = 1_800_000_000
  const start = boundary - CANDLE_COUNT * CANDLE_SECONDS
  const candles = Array.from({ length: CANDLE_COUNT }, (_, index) => ({
    t: start + index * CANDLE_SECONDS,
    sum: '100000',
  }))
  candles.push({ t: boundary, sum: '999999999' })
  assert.equal(sumTwelveHours(candles, boundary), 14_400_000)
  assert.equal(sumTwelveHours([{ t: start, sum: '-1' }], boundary), null)
  assert.equal(sumTwelveHours([candles[0], candles[0]], boundary), null)
})

test('24 小时预筛后按 12 小时成交额选币，单币种失败保留旧名单', async () => {
  const boundary = 1_800_000_000
  const previous = new Map([['ETH/USDT:USDT', { symbol: 'ETH/USDT:USDT', turnover: 12_000_000 }]])
  const fake = {
    markets: {
      BTC: { id: 'BTC_USDT', symbol: 'BTC/USDT:USDT', active: true, swap: true, linear: true, settle: 'USDT' },
      ETH: { id: 'ETH_USDT', symbol: 'ETH/USDT:USDT', active: true, swap: true, linear: true, settle: 'USDT' },
    },
    publicFuturesGetSettleTickers: async () => [
      { contract: 'BTC_USDT', volume_24h_quote: '20000000' },
      { contract: 'ETH_USDT', volume_24h_quote: '30000000' },
    ],
    publicFuturesGetSettleCandlesticks: async ({ contract }: { contract: string }) => {
      if (contract === 'ETH_USDT') throw new Error('网络错误')
      return [{ t: boundary - CANDLE_SECONDS, sum: '11000000' }]
    },
  } as unknown as InstanceType<typeof ccxt.gate>

  const originalError = console.error
  console.error = () => {}
  try {
    const actual = await findQualifiedMarkets(fake, 10_000_000, boundary, previous)
    assert.equal(actual.get('BTC/USDT:USDT')?.turnover, 11_000_000)
    assert.equal(actual.get('ETH/USDT:USDT')?.turnover, 12_000_000)
  } finally {
    console.error = originalError
  }
})
