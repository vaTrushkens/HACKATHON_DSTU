import { chartTooltip } from '../chartTheme'
import Select from '../components/Select'
import ReactECharts from 'echarts-for-react'
import { useEffect, useState } from 'react'
import { get } from '../api'
import { useAuth } from '../auth'
import { fmtDate, fmtMoney, fmtShort, sourceLabel } from '../format'

const css = (n: string, f: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f

export default function PlanFact() {
  const { company } = useAuth()
  const [data, setData] = useState<any>(null)
  const [runs, setRuns] = useState<any[]>([])
  const [runId, setRunId] = useState<number | ''>('')

  useEffect(() => { if (company) get(`/companies/${company.id}/forecast-runs/`).then(setRuns) }, [company?.id])
  useEffect(() => { if (company) get(`/companies/${company.id}/plan-fact/`, runId ? { run_id: runId } : {}).then(setData) }, [company?.id, runId])

  if (!company) return null
  const weeks: any[] = data?.weeks ?? []
  const blue = css('--series-1', '#2a78d6'), orange = css('--series-2', '#eb6834'), ink = css('--text-2', '#52514e'), grid = css('--grid', '#e1e0d9'), muted = css('--muted', '#898781'), soft = css('--series-1-soft', 'rgba(42,120,214,0.14)')

  const weekOpt = (planKey: string, factKey: string, title: string) => ({
    title: { text: title, left: 0, textStyle: { fontSize: 13, color: ink, fontWeight: 600 } },
    grid: { left: 56, right: 12, top: 36, bottom: 28 },
    tooltip: { ...chartTooltip, trigger: 'axis', formatter: (p: any[]) => `<b>неделя до ${fmtDate(p[0].axisValue)}</b><br/>` + p.map((x) => `${x.marker} ${x.seriesName}: <b>${x.value === null || x.value === undefined ? '—' : fmtMoney(x.value)}</b>`).join('<br/>') },
    legend: { bottom: 0, textStyle: { color: ink, fontSize: 11 }, icon: 'roundRect' },
    xAxis: { type: 'category', data: weeks.map((w) => w.week_end), axisLabel: { color: muted, hideOverlap: true, formatter: (v: string) => fmtDate(v).slice(0, 6) }, axisLine: { lineStyle: { color: grid } } },
    yAxis: { type: 'value', axisLabel: { color: muted, hideOverlap: true, formatter: (v: number) => fmtShort(v) }, splitLine: { lineStyle: { color: grid } } },
    series: [
      { name: 'План', type: 'bar', data: weeks.map((w) => w[planKey]), itemStyle: { color: soft, borderColor: blue, borderWidth: 1.5, borderRadius: [4, 4, 0, 0] }, barGap: '10%' },
      { name: 'Факт', type: 'bar', data: weeks.map((w) => w[factKey]), itemStyle: { color: blue, borderRadius: [4, 4, 0, 0] } },
    ],
  })
  const daily: any[] = data?.daily ?? []
  const balOpt = {
    grid: { left: 56, right: 12, top: 12, bottom: 28 },
    tooltip: { ...chartTooltip, trigger: 'axis', formatter: (p: any[]) => `<b>${fmtDate(p[0].axisValue)}</b><br/>` + p.filter((x) => x.value?.[1] !== null && x.value?.[1] !== undefined && x.seriesName !== 'p10' && x.seriesName !== 'коридор').map((x) => `${x.marker} ${x.seriesName}: <b>${fmtMoney(x.value[1])}</b>`).join('<br/>') },
    xAxis: { type: 'time', axisLabel: { color: muted, hideOverlap: true }, axisLine: { lineStyle: { color: grid } } },
    yAxis: { type: 'value', axisLabel: { color: muted, hideOverlap: true, formatter: (v: number) => fmtShort(v) }, splitLine: { lineStyle: { color: grid } } },
    series: [
      { name: 'p10', type: 'line', stack: 'b', data: daily.map((d) => [d.date, d.p10]), showSymbol: false, lineStyle: { opacity: 0 }, silent: true },
      { name: 'коридор', type: 'line', stack: 'b', data: daily.map((d) => [d.date, d.p90 - d.p10]), showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { color: blue, opacity: 0.14 }, silent: true },
      { name: 'Прогноз (медиана)', type: 'line', data: daily.map((d) => [d.date, d.p50]), showSymbol: false, lineStyle: { color: blue, width: 2.5 }, itemStyle: { color: blue } },
      { name: 'Факт', type: 'line', data: daily.filter((d) => d.balance !== null).map((d) => [d.date, d.balance]), showSymbol: false, lineStyle: { color: ink, width: 2 }, itemStyle: { color: ink } },
    ],
  }
  return (
    <>
      <div className="page-head">
        <div><h1>План-факт</h1><p className="subtitle">Сохранённый прогноз против того, что произошло. Расхождения подсвечены; ожидаемые платежи, которые не пришли в срок, помечаются и прогноз пересчитывается без них.</p></div>
        <label className="field">Снапшот прогноза<Select value={runId} onValueChange={(value) => setRunId(value ? Number(value) : '')}><option value="">автоматически (самый свежий с ≥ 7 дн. факта)</option>{runs.map((r) => <option key={r.id} value={r.id}>от {fmtDate(r.as_of)} · {r.horizon_days} дн. · {r.risk?.summary}</option>)}</Select></label>
      </div>
      {data?.detail && <div className="card">{data.detail}<div className="small muted" style={{ marginTop: 6 }}>Подсказка для демо: на «Обзоре» поставьте дату «сегодня» на месяц раньше последней операции и нажмите «Применить» — снапшот сохранится, а факт за этот месяц станет сравнением.</div></div>}
      {data?.run && (
        <div className="grid">
          <div className="card tight small muted">Прогноз от {fmtDate(data.run.as_of)} ({data.run.model}) · факт до {fmtDate(data.fact_until)} · тогда сервис оценивал: <b>{data.run.risk?.summary}</b></div>
          <div className="card"><h2>Остаток: прогноз и факт по дням</h2><ReactECharts option={balOpt} style={{ height: 300 }} notMerge /></div>
          <div className="grid grid-2">
            <div className="card"><ReactECharts option={weekOpt('plan_in', 'fact_in', 'Поступления по неделям')} style={{ height: 260 }} notMerge /></div>
            <div className="card"><ReactECharts option={weekOpt('plan_out', 'fact_out', 'Списания по неделям')} style={{ height: 260 }} notMerge /></div>
          </div>
          <div className="card">
            <h2>Отклонения по неделям</h2>
            <table><thead><tr><th>Неделя до</th><th className="num">План поступлений</th><th className="num">Факт</th><th className="num">Δ</th><th className="num">План списаний</th><th className="num">Факт</th><th className="num">Δ</th><th className="num">Остаток: план</th><th className="num">факт</th><th className="num">Δ</th></tr></thead>
              <tbody>{weeks.map((w) => <tr key={w.week_end}><td>{fmtDate(w.week_end)}</td><td className="num">{fmtMoney(w.plan_in)}</td><td className="num">{w.has_fact ? fmtMoney(w.fact_in) : '—'}</td><td className={`num ${w.dev_in < 0 ? 'neg' : 'pos'}`}>{w.has_fact ? fmtMoney(w.dev_in) : ''}</td><td className="num">{fmtMoney(w.plan_out)}</td><td className="num">{w.has_fact ? fmtMoney(w.fact_out) : '—'}</td><td className={`num ${w.dev_out > 0 ? 'neg' : 'pos'}`}>{w.has_fact ? fmtMoney(w.dev_out) : ''}</td><td className="num">{fmtMoney(w.plan_balance)}</td><td className="num">{w.has_fact ? fmtMoney(w.fact_balance) : '—'}</td><td className={`num ${w.dev_balance < 0 ? 'neg' : 'pos'}`}>{w.has_fact ? fmtMoney(w.dev_balance) : ''}</td></tr>)}</tbody></table>
          </div>
          <div className="card">
            <h2>Ожидаемые события: пришли или нет</h2>
            {data.events.length === 0 ? <div className="empty">В этом периоде не было плановых событий (счетов, ручных событий)</div> : (
              <table><thead><tr><th>Дата</th><th>Событие</th><th>Источник</th><th className="num">Сумма</th><th>Статус</th></tr></thead>
                <tbody>{data.events.map((e: any) => <tr key={e.id}><td>{fmtDate(e.date)}</td><td>{e.label}<div className="small muted">{e.counterparty_name}</div></td><td><span className="source-tag">{sourceLabel(e.source)}</span></td><td className={`num ${e.direction === 'Поступление' ? 'pos' : 'neg'}`}>{fmtMoney(e.amount)}</td><td><span className={`badge ${e.status === 'overdue' ? 'high' : e.status === 'matched' ? 'good' : 'info'}`}>{e.status === 'matched' ? `исполнено ${e.matched_date ? fmtDate(e.matched_date) : ''}` : e.status === 'overdue' ? 'не пришло в срок' : 'ожидается'}</span></td></tr>)}</tbody></table>
            )}
          </div>
        </div>
      )}
    </>
  )
}
