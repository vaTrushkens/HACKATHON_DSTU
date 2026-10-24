import ReactECharts from 'echarts-for-react'
import { useMemo } from 'react'
import type { DailyPoint, ForecastEvent, HistoryPoint } from '../types'
import { fmtDate, fmtMoney, fmtShort, sourceLabel } from '../format'

type Props = {
  daily: DailyPoint[]
  events?: ForecastEvent[]
  history?: HistoryPoint[]        // факт до as_of
  actual?: HistoryPoint[]         // факт после as_of («что было на самом деле»)
  compare?: DailyPoint[]          // второй прогноз (сценарий)
  compareLabel?: string
  minBalance?: number
  height?: number
}

const css = (name: string, fallback: string) => (typeof window !== 'undefined' ? getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback : fallback)

export default function BalanceChart({ daily, events = [], history = [], actual = [], compare, compareLabel = 'Сценарий', minBalance = 0, height = 360 }: Props) {
  const option = useMemo(() => {
    const blue = css('--series-1', '#2a78d6')
    const orange = css('--series-2', '#eb6834')
    const violet = css('--series-7', '#6250d6')
    const ink2 = css('--text-2', '#52514e')
    const muted = css('--muted', '#898781')
    const grid = css('--grid', '#e1e0d9')
    const critical = css('--critical', '#d03b3b')

    const histData = history.map((h) => [h.date, h.balance])
    const asOf = daily[0]?.date
    const bridge = history.length ? [[history[history.length - 1].date, history[history.length - 1].balance]] : []
    const p50 = [...bridge, ...daily.map((d) => [d.date, d.p50])]
    const p10 = daily.map((d) => [d.date, d.p10])
    const band = daily.map((d) => [d.date, d.p90 - d.p10])
    const actualData = actual.map((a) => [a.date, a.balance])
    const cmp = compare ? [...bridge, ...compare.map((d) => [d.date, d.p50])] : []

    const bigEvents = events.filter((e) => e.amount > 0).sort((a, b) => b.amount - a.amount).slice(0, 12)
    const byDate = new Map(daily.map((d) => [d.date, d]))
    const markers = bigEvents.map((e) => ({
      name: e.label, value: [e.date, byDate.get(e.date)?.p50 ?? 0], symbol: e.direction === 'Поступление' ? 'triangle' : 'diamond', symbolSize: 10,
      itemStyle: { color: e.direction === 'Поступление' ? css('--series-3', '#1baf7a') : orange, borderColor: css('--surface', '#fcfcfb'), borderWidth: 2 }, ev: e,
    }))

    const series: any[] = [
      { name: 'Факт', type: 'line', data: histData, showSymbol: false, lineStyle: { width: 2, color: ink2 }, itemStyle: { color: ink2 }, z: 3 },
      { name: 'p10', type: 'line', data: p10, stack: 'band', showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
      { name: 'Коридор 10–90%', type: 'line', data: band, stack: 'band', showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { color: blue, opacity: 0.14 }, silent: true, tooltip: { show: false } },
      { name: 'Прогноз (медиана)', type: 'line', data: p50, showSymbol: false, lineStyle: { width: 2.5, color: blue }, itemStyle: { color: blue }, z: 4 },
    ]
    if (cmp.length) series.push({ name: compareLabel, type: 'line', data: cmp, showSymbol: false, lineStyle: { width: 2.5, color: violet, type: [6, 4] }, itemStyle: { color: violet }, z: 4 })
    if (actualData.length) series.push({ name: 'Что было на самом деле', type: 'line', data: actualData, showSymbol: false, lineStyle: { width: 2, color: ink2, type: 'dotted' }, itemStyle: { color: ink2 }, z: 5 })
    series.push({ name: 'События', type: 'scatter', data: markers, z: 6, tooltip: { formatter: (p: any) => { const e: ForecastEvent = p.data.ev; return `<b>${e.label}</b><br/>${fmtDate(e.date)} · ${e.direction === 'Поступление' ? '+' : '−'}${fmtMoney(e.amount)}<br/><span style="color:${muted}">${sourceLabel(e.source)}</span>` } } })
    series.push({
      name: 'Порог', type: 'line', data: [], markLine: {
        silent: true, symbol: 'none', lineStyle: { color: critical, type: 'dashed', width: 1.5 },
        data: [{ yAxis: minBalance, label: { formatter: minBalance ? `Минимум ${fmtShort(minBalance)}` : 'Ноль', position: 'insideEndTop', color: critical, fontSize: 11 } }, ...(asOf ? [{ xAxis: asOf, lineStyle: { color: muted, type: 'solid' }, label: { formatter: 'сегодня', position: 'insideEndTop', color: muted, fontSize: 11 } }] : [])],
      },
    })
    return {
      animationDuration: 300,
      grid: { left: 64, right: 24, top: 24, bottom: 40 },
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: muted } },
        formatter: (params: any[]) => {
          const date = params[0]?.axisValue
          const d = byDate.get(date)
          const lines = [`<b>${fmtDate(date)}</b>`]
          params.filter((p) => p.seriesType === 'line' && p.value?.[1] !== undefined && !['p10', 'Коридор 10–90%'].includes(p.seriesName)).forEach((p) => lines.push(`${p.marker} ${p.seriesName}: <b>${fmtMoney(p.value[1])}</b>`))
          if (d) {
            lines.push(`<span style="color:${muted}">коридор ${fmtShort(d.p10)} … ${fmtShort(d.p90)}</span>`)
            if (d.prob_below > 0.02) lines.push(`<span style="color:${critical}">вероятность ниже порога: ${Math.round(d.prob_below * 100)}%</span>`)
          }
          return lines.join('<br/>')
        },
      },
      xAxis: { type: 'time', axisLine: { lineStyle: { color: grid } }, axisLabel: { color: muted, formatter: (v: number) => { const dt = new Date(v); return `${dt.getDate()}.${String(dt.getMonth() + 1).padStart(2, '0')}` } }, splitLine: { show: false } },
      yAxis: { type: 'value', axisLabel: { color: muted, formatter: (v: number) => fmtShort(v) }, splitLine: { lineStyle: { color: grid } }, axisLine: { show: false } },
      series,
    }
  }, [daily, events, history, actual, compare, compareLabel, minBalance])

  return (
    <div>
      <ReactECharts option={option} style={{ height }} notMerge />
      <div className="legend">
        <span><i style={{ background: 'var(--text-2)' }} />Факт</span>
        <span><i style={{ background: 'var(--series-1)' }} />Прогноз (медиана)</span>
        <span><i style={{ background: 'var(--series-1-soft)', height: 8 }} />Коридор 10–90%</span>
        {compare && <span><i style={{ background: 'var(--series-7)' }} />{compareLabel}</span>}
        {actual.length > 0 && <span><i style={{ background: 'var(--text-2)', height: 2, borderTop: '2px dotted var(--text-2)', backgroundColor: 'transparent' }} />Что было на самом деле</span>}
        <span>◆ списание · ▲ поступление (крупные события)</span>
      </div>
    </div>
  )
}
