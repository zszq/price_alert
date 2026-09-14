function positiveNumber(name: string, fallback: number): number {
  const raw = process.env[name]
  if (raw === undefined) return fallback
  const value = Number(raw)
  // 非法阈值若被静默采用，会让监控无报警或持续误报，因此在启动时直接拒绝。
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} 必须是正数`)
  return value
}

export const config = {
  minTurnover: positiveNumber('MIN_12H_TURNOVER_USDT', 10_000_000),
  sampleMs: 5_000,
  oneMinuteFloor: positiveNumber('ALERT_1M_PERCENT', 1) / 100,
  fiveMinuteFloor: positiveNumber('ALERT_5M_PERCENT', 2) / 100,
  sigmaMultiplier: positiveNumber('ALERT_SIGMA', 3),
  cooldownMs: positiveNumber('ALERT_COOLDOWN_SECONDS', 300) * 1_000,
}
