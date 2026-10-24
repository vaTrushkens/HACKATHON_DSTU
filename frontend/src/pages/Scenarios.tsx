import { useEffect, useState } from 'react'
import { get, post } from '../api'
import { useAuth } from '../auth'
import BalanceChart from '../components/BalanceChart'
import { addDays, fmtDate, fmtMoney, fmtPct } from '../format'
import type { ForecastResponse, HistoryPoint, Scenario } from '../types'

const PRESETS: { name: string; sc: Scenario }[] = [
  { name: 'Крупный клиент заплатит на 3 недели позже', sc: { delay_client_days: 21 } },
  { name: 'Выручка упадёт на 20%', sc: { revenue_change_pct: -20 } },
  { name: 'Внеплановый расход 500 000 ₽ через неделю', sc: { extra_expenses: [{ date: '', amount: 500000, label: 'Ремонт оборудования' }] } },
  { name: 'Аренда и постоянные расходы +15%', sc: { fixed_cost_change_pct: 15 } },
  { name: 'Всё сразу (стресс-тест)', sc: { delay_client_days: 14, revenue_change_pct: -15, fixed_cost_change_pct: 10 } },
]

export default function Scenarios() {
  const { company } = useAuth()
  const [sc, setSc] = useState<Scenario>({ revenue_change_pct: 0, fixed_cost_change_pct: 0, delay_client_days: 0, delay_client_key: '', extra_expenses: [] })
  const [extra, setExtra] = useState({ date: '', amount: '', label: 'Внеплановый расход' })
  const [clients, setClients] = useState<any[]>([])
  const [res, setRes] = useState<{ base: ForecastResponse; scenario: ForecastResponse; delta: any } | null>(null)
  const [history, setHistory] = useState<HistoryPoint[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => { if (company) get(`/companies/${company.id}/counterparties/`).then((r) => setClients(r.clients ?? [])) }, [company?.id])

  const run = async (override?: Scenario) => {
    if (!company) return
    const s = override ?? sc
    setBusy(true)
    try {
      const r = await post(`/companies/${company.id}/scenario/`, { scenario: { ...s, name: 'Негативный сценарий' }, horizon: 60 })
      setRes(r)
      const a = r.base.forecast.as_of
      setHistory(await get(`/companies/${company.id}/history/`, { from: addDays(a, -60), to: a }))
    } finally { setBusy(false) }
  }
  useEffect(() => { run() }, [company?.id])

  const applyPreset = (p: Scenario) => {
    const asOf = res?.base.forecast.as_of ?? new Date().toISOString().slice(0, 10)
    const s: Scenario = { ...p, extra_expenses: (p.extra_expenses ?? []).map((e) => ({ ...e, date: e.date || addDays(asOf, 7) })) }
    setSc({ revenue_change_pct: 0, fixed_cost_change_pct: 0, delay_client_days: 0, delay_client_key: '', ...s }); run(s)
  }
  const addExtra = () => {
    if (!extra.date || !extra.amount) return
    const s = { ...sc, extra_expenses: [...(sc.extra_expenses ?? []), { date: extra.date, amount: Number(extra.amount), label: extra.label }] }
    setSc(s); setExtra({ date: '', amount: '', label: 'Внеплановый расход' })
  }

  if (!company) return null
  const b = res?.base, s = res?.scenario
  return (
    <>
      <div className="page-head"><div><h1>Что если</h1><p className="subtitle">Задайте неблагоприятное событие и сразу увидите, как изменится прогноз и появится ли кассовый разрыв. Базовый и негативный сценарии — на одном графике.</p></div></div>
      <div className="grid" style={{ gridTemplateColumns: '320px 1fr' }}>
        <div className="grid">
          <div className="card">
            <h2>Готовые сценарии</h2>
            {PRESETS.map((p) => <button key={p.name} className="secondary" style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 6 }} onClick={() => applyPreset(p.sc)}>{p.name}</button>)}
          </div>
          <div className="card">
            <h2>Свой сценарий</h2>
            <label className="field">Выручка: {sc.revenue_change_pct! > 0 ? '+' : ''}{sc.revenue_change_pct}%<input type="range" min={-50} max={30} step={5} value={sc.revenue_change_pct} onChange={(e) => setSc({ ...sc, revenue_change_pct: Number(e.target.value) })} /></label>
            <label className="field" style={{ marginTop: 8 }}>Постоянные расходы: {sc.fixed_cost_change_pct! > 0 ? '+' : ''}{sc.fixed_cost_change_pct}%<input type="range" min={-20} max={50} step={5} value={sc.fixed_cost_change_pct} onChange={(e) => setSc({ ...sc, fixed_cost_change_pct: Number(e.target.value) })} /></label>
            <label className="field" style={{ marginTop: 8 }}>Задержка оплаты клиентами: {sc.delay_client_days} дн.<input type="range" min={0} max={45} step={7} value={sc.delay_client_days} onChange={(e) => setSc({ ...sc, delay_client_days: Number(e.target.value) })} /></label>
            <label className="field" style={{ marginTop: 8 }}>Какой клиент<select value={sc.delay_client_key ?? ''} onChange={(e) => setSc({ ...sc, delay_client_key: e.target.value || null })}><option value="">Все ожидаемые поступления от клиентов</option>{clients.slice(0, 10).map((c) => <option key={c.inn + c.name} value={c.name}>{c.name} ({fmtPct(c.share)})</option>)}</select></label>
            <h3 style={{ marginTop: 12 }}>Внеплановый расход</h3>
            <div className="row"><input type="date" value={extra.date} onChange={(e) => setExtra({ ...extra, date: e.target.value })} /><input type="number" placeholder="Сумма" value={extra.amount} onChange={(e) => setExtra({ ...extra, amount: e.target.value })} style={{ width: 120 }} /></div>
            <div className="row" style={{ marginTop: 6 }}><input value={extra.label} onChange={(e) => setExtra({ ...extra, label: e.target.value })} style={{ flex: 1 }} /><button className="secondary" onClick={addExtra}>+</button></div>
            {(sc.extra_expenses ?? []).map((e, i) => <div key={i} className="small muted">• {fmtDate(e.date)} — {e.label}: {fmtMoney(e.amount)} <button className="ghost" onClick={() => setSc({ ...sc, extra_expenses: sc.extra_expenses!.filter((_, j) => j !== i) })}>×</button></div>)}
            <button style={{ marginTop: 12, width: '100%' }} onClick={() => run()} disabled={busy}>{busy ? 'Считаем…' : 'Пересчитать прогноз'}</button>
          </div>
        </div>
        <div className="grid">
          {b && s && (
            <>
              <div className="kpi-row">
                <div className="card stat"><span className="label">Вероятность разрыва</span><span className="value">{fmtPct(b.risk.probability)} → <span className={s.risk.probability > b.risk.probability ? 'neg' : 'pos'}>{fmtPct(s.risk.probability)}</span></span></div>
                <div className="card stat"><span className="label">Дата разрыва</span><span className="value" style={{ fontSize: 18 }}>{b.risk.gap_date ? fmtDate(b.risk.gap_date) : '—'} → <span className="neg">{s.risk.gap_date ? fmtDate(s.risk.gap_date) : '—'}</span></span></div>
                <div className="card stat"><span className="label">Нехватка (ожидаемо)</span><span className="value" style={{ fontSize: 18 }}>{fmtMoney(b.risk.deficit_p50)} → <span className="neg">{fmtMoney(s.risk.deficit_p50)}</span></span></div>
                <div className="card stat"><span className="label">Остаток через 60 дней</span><span className="value" style={{ fontSize: 18 }}>{fmtMoney(b.forecast.components.end_balance_p50)} → <span className={res!.delta.end_balance_p50 < 0 ? 'neg' : 'pos'}>{fmtMoney(s.forecast.components.end_balance_p50)}</span></span></div>
              </div>
              <div className="card">
                <h2>Базовый и негативный прогноз</h2>
                <BalanceChart daily={b.forecast.daily} compare={s.forecast.daily} compareLabel="Негативный сценарий" events={s.forecast.events} history={history} minBalance={b.forecast.min_balance} />
              </div>
              <div className={`card risk-card ${s.risk.level}`}><div className="risk-summary">{s.risk.summary}</div><p className="explain">{s.risk.explanation}</p></div>
            </>
          )}
          {!res && <div className="empty">Считаем базовый сценарий…</div>}
        </div>
      </div>
    </>
  )
}
