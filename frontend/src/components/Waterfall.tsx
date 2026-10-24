import ReactECharts from 'echarts-for-react'
import type { Risk } from '../types'
import { fmtMoney, fmtShort } from '../format'

const css = (n: string, f: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f

/** Водопад «от остатка сегодня к остатку на дату риска»: почему денег не хватит. */
export default function Waterfall({ rows }: { rows: Risk['waterfall'] }) {
  if (!rows?.length) return null
  const labels = rows.map((r) => r.label.length > 34 ? r.label.slice(0, 32) + '…' : r.label)
  let running = 0
  const base: number[] = []
  const vals: number[] = []
  const colors: string[] = []
  const blue = css('--series-1', '#2a78d6'), orange = css('--series-2', '#eb6834'), aqua = css('--series-3', '#1baf7a'), ink = css('--text-2', '#52514e')
  rows.forEach((r) => {
    if (r.kind === 'start' || r.kind === 'end') { base.push(Math.min(0, r.amount)); vals.push(Math.abs(r.amount)); colors.push(r.kind === 'start' ? ink : blue); running = r.amount; return }
    const next = running + r.amount
    base.push(Math.min(running, next)); vals.push(Math.abs(r.amount)); colors.push(r.amount >= 0 ? aqua : orange); running = next
  })
  const option = {
    grid: { left: 8, right: 60, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (p: any[]) => { const i = p[0].dataIndex; const r = rows[i]; return `<b>${r.label}</b><br/>${fmtMoney(r.amount)}${r.note ? `<br/><span style="color:${css('--muted', '#898781')}">${r.note}</span>` : ''}` } },
    xAxis: { type: 'value', axisLabel: { formatter: (v: number) => fmtShort(v), color: css('--muted', '#898781') }, splitLine: { lineStyle: { color: css('--grid', '#e1e0d9') } } },
    yAxis: { type: 'category', data: labels, inverse: true, axisLabel: { color: css('--text-2', '#52514e'), fontSize: 12 }, axisLine: { show: false }, axisTick: { show: false } },
    series: [
      { type: 'bar', stack: 'w', data: base, itemStyle: { color: 'transparent' }, silent: true, tooltip: { show: false }, barWidth: 16 },
      { type: 'bar', stack: 'w', data: vals.map((v, i) => ({ value: v, itemStyle: { color: colors[i], borderRadius: 4 } })), barWidth: 16, label: { show: true, position: 'right', color: css('--text-2', '#52514e'), fontSize: 11, formatter: (p: any) => fmtShort(rows[p.dataIndex].amount) } },
    ],
  }
  return <ReactECharts option={option} style={{ height: 36 * rows.length + 30 }} notMerge />
}
