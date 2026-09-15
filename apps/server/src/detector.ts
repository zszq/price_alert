import { config } from './config.js'

export type Alert = {
  symbol: string
  direction: '上涨' | '下跌'
  windowMinutes: 1 | 5
  price: number
  change: number
  threshold: number
  turnover: number
  time: number
}

type Sample = { time: number; price: number }
type State = { samples: Sample[]; lastAlert: Map<string, number> }

export class PriceDetector {
  private readonly states = new Map<string, State>()

  /** 清除已退订交易对的历史样本和冷却状态，防止重新订阅时沿用过期基线。 */
  remove(symbol: string): void {
    this.states.delete(symbol)
  }

  /** 把当前价格与历史波动比较，动态门槛和绝对门槛同时生效以压低误报。 */
  sample(symbol: string, price: number, turnover: number, time: number): Alert | null {
    if (!Number.isFinite(price) || price <= 0) return null
    const state = this.states.get(symbol) ?? { samples: [], lastAlert: new Map<string, number>() }
    if (!this.states.has(symbol)) this.states.set(symbol, state)
    const samples = state.samples
    const last = samples.at(-1)
    if (last && time <= last.time) return null
    // 断流后旧价格不再代表一分钟前的价格，跨缺口比较会制造虚假的瞬时异动。
    if (last && time - last.time > 30_000) samples.length = 0
    const previous = samples.at(-1)
    if (previous && time - previous.time < config.sampleMs) return null

    // 基线只取本次价格到来前的收益率，防止异常点先抬高自身的判定门槛。
    const returns = samples.slice(1).map((item, index) => Math.log(item.price / samples[index].price))
    const baseline = returns.slice(-120)
    const sigma = baseline.length >= 12 ? standardDeviation(baseline) : 0
    const options = [
      { minutes: 1 as const, floor: config.oneMinuteFloor },
      { minutes: 5 as const, floor: config.fiveMinuteFloor },
    ]
    let alert: Alert | null = null

    for (const option of options) {
      const target = time - option.minutes * 60_000
      // 时间容差仅覆盖定时器轻微漂移；更旧的样本会把过长区间误判成短时波动。
      const reference = [...samples].reverse().find(item => item.time <= target && item.time >= target - 10_000)
      if (!reference) continue
      const change = price / reference.price - 1
      const dynamic = config.sigmaMultiplier * sigma * Math.sqrt((option.minutes * 60_000) / config.sampleMs)
      const threshold = Math.max(option.floor, Math.expm1(dynamic))
      if (Math.abs(change) < threshold) continue
      const direction = change > 0 ? '上涨' : '下跌'
      // 同方向的 1 分钟和 5 分钟报警共用冷却，避免同一次行情异动连续提醒。
      const key = direction
      if (time - (state.lastAlert.get(key) ?? -Infinity) < config.cooldownMs) continue
      state.lastAlert.set(key, time)
      alert = { symbol, direction, windowMinutes: option.minutes, price, change, threshold, turnover, time }
      break
    }

    samples.push({ time, price })
    // 120 个五秒收益率约需十分钟历史；多保留一分钟以吸收采样漂移和边界容差。
    while (samples.length && samples[0].time < time - 11 * 60_000) samples.shift()
    return alert
  }
}

/** 计算总体标准差；这里的完整历史窗口就是总体，不需要样本自由度修正。 */
function standardDeviation(values: number[]): number {
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length
  const variance = values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length
  return Math.sqrt(variance)
}
