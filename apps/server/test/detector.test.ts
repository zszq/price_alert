import assert from 'node:assert/strict'
import { test } from 'node:test'
import { PriceDetector } from '../src/detector.js'

test('检测 1 分钟突涨并对同向报警冷却', () => {
  const detector = new PriceDetector()
  const start = 1_800_000_000_000
  for (let index = 0; index <= 60; index++) {
    assert.equal(detector.sample('BTC/USDT:USDT', 100, 20_000_000, start + index * 5_000), null)
  }
  const alert = detector.sample('BTC/USDT:USDT', 102, 20_000_000, start + 61 * 5_000)
  assert.equal(alert?.direction, '上涨')
  assert.equal(alert?.windowMinutes, 1)
  assert.ok(alert && Math.abs(alert.change - 0.02) < 1e-12)
  assert.equal(detector.sample('BTC/USDT:USDT', 103, 20_000_000, start + 62 * 5_000), null)
})

test('移除交易对后清除价格历史', () => {
  const detector = new PriceDetector()
  const start = 1_800_000_000_000
  for (let index = 0; index <= 12; index++) detector.sample('ETH/USDT:USDT', 100, 11_000_000, start + index * 5_000)
  detector.remove('ETH/USDT:USDT')
  assert.equal(detector.sample('ETH/USDT:USDT', 80, 11_000_000, start + 65_000), null)
  assert.equal(detector.sample('ETH/USDT:USDT', NaN, 11_000_000, start + 70_000), null)
})

test('高波动基线抑制小幅波动，断流后不跨时间窗口比较', () => {
  const detector = new PriceDetector()
  const start = 1_800_000_000_000
  for (let index = 0; index <= 120; index++) {
    assert.equal(detector.sample('BTC/USDT:USDT', index % 2 ? 100.5 : 100, 20_000_000, start + index * 5_000), null)
  }
  assert.equal(detector.sample('BTC/USDT:USDT', 102, 20_000_000, start + 121 * 5_000), null)
  assert.equal(detector.sample('BTC/USDT:USDT', 120, 20_000_000, start + 121 * 5_000 + 31_000), null)
})
