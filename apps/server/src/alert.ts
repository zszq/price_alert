import { spawn } from 'node:child_process'
import type { Alert } from './detector.js'

let lastSoundAt = 0

export function announce(alert: Alert): void {
  const change = `${alert.change >= 0 ? '+' : ''}${(alert.change * 100).toFixed(2)}%`
  const threshold = `${(alert.threshold * 100).toFixed(2)}%`
  console.warn(
    `[价格异动] ${new Date(alert.time).toISOString()} ${alert.symbol} ${alert.direction} ` +
      `${alert.windowMinutes}分钟 ${change} (阈值 ${threshold}) ` +
      `现价 ${alert.price} 12小时成交额 ${alert.turnover.toFixed(0)} USDT`,
  )
  // 多个交易对同时触发时仍逐条打印，但短时间只播放一次，避免声音叠加。
  if (Date.now() - lastSoundAt < 2_000) return
  lastSoundAt = Date.now()
  process.stdout.write('\x07')

  if (process.platform === 'win32') {
    // 部分终端会禁用 BEL；Windows 下再请求系统蜂鸣，确保有独立的声音路径。
    const sound = spawn('powershell.exe', ['-NoProfile', '-Command', '[console]::Beep(880,250)'], {
      stdio: 'ignore',
      windowsHide: true,
    })
    sound.on('error', error => console.error('[提示音失败]', error))
    sound.unref()
  }
}
