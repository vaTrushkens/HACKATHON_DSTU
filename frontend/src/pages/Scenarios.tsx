import RangeField from '../components/RangeField'
import Select from '../components/Select'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, get } from '../api'
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
  const [error, setError] = useState('')
  const companyId = company?.id
  const activeRequest = useRef<AbortController | null>(null)
  const [editing, setEditing] = useState(false)
  const currentScenario = useRef(sc)
  currentScenario.current = sc

  useEffect(() => { if (company) get(`/companies/${company.id}/counterparties/`).then((r) => setClients(r.clients ?? [])) }, [company?.id])

  const run = useCallback(async (scenario: Scenario) => {
    if (!companyId) return
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy(true); setEditing(false); setError('')
    try {
      const { data } = await api.post<{ base: ForecastResponse; scenario: ForecastResponse; delta: any }>(`/companies/${companyId}/scenario/`,
        { scenario: { ...scenario, name: 'Негативный сценарий' }, horizon: 60 }, { signal: controller.signal })
      const asOf = data.base.forecast.as_of
      const { data: history } = await api.get<HistoryPoint[]>(`/companies/${companyId}/history/`,
        { params: { from: addDays(asOf, -60), to: asOf }, signal: controller.signal })
      if (!controller.signal.aborted) { setRes(data); setHistory(history) }
    } catch {
      if (!controller.signal.aborted) setError('Не удалось обновить прогноз. Попробуйте пересчитать ещё раз.')
    } finally {
      if (activeRequest.current === controller) setBusy(false)
    }
  }, [companyId])

  useEffect(() => {
    setRes(null); setHistory([])
    run(currentScenario.current)
    return () => activeRequest.current?.abort()
  }, [run])

  const previewScenario = (scenario: Scenario) => {
    activeRequest.current?.abort()
    activeRequest.current = null
    setBusy(false); setEditing(true); setSc(scenario)
  }
  const commitScenario = (scenario: Scenario) => { setSc(scenario); run(scenario) }

  const applyPreset = (p: Scenario) => {
    const asOf = res?.base.forecast.as_of ?? new Date().toISOString().slice(0, 10)
    const s: Scenario = { ...p, extra_expenses: (p.extra_expenses ?? []).map((e) => ({ ...e, date: e.date || addDays(asOf, 7) })) }
    commitScenario({ revenue_change_pct: 0, fixed_cost_change_pct: 0, delay_client_days: 0, delay_client_key: '', ...s })
  }
  const addExtra = () => {
    if (!extra.date || Number(extra.amount) <= 0) return
    const s = { ...sc, extra_expenses: [...(sc.extra_expenses ?? []), { date: extra.date, amount: Number(extra.amount), label: extra.label }] }
    commitScenario(s); setExtra({ date: '', amount: '', label: 'Внеплановый расход' })
  }

  if (!company) return null
  const b = res?.base, s = res?.scenario
  return (
    <>
      <div className="page-head"><div><h1>Что если</h1><p className="subtitle">Задайте неблагоприятное событие и сразу увидите, как изменится прогноз и появится ли кассовый разрыв. Базовый и негативный сценарии — на одном графике.</p></div></div>
      <section className="scenario-presets" aria-label="Готовые сценарии">
        <h2>Готовые сценарии</h2>
        <div className="preset-list">{PRESETS.map((p, i) => <button key={p.name} className="preset-card" disabled={busy} onClick={() => applyPreset(p.sc)}><span className="preset-icon" aria-hidden="true">{['↗', '%', '₽', '+', '↔'][i]}</span><span>{p.name}</span></button>)}</div>
      </section>
      <div className="scenario-calculator">
        <h2 className="calculator-title">Рассчитать свой сценарий</h2>
        <div className="scenario-controls">
          <div className="scenario-form">

            <RangeField label="Изменение выручки" min={-50} max={30} step={5} value={sc.revenue_change_pct ?? 0} displayValue={`${(sc.revenue_change_pct ?? 0) > 0 ? '+' : ''}${sc.revenue_change_pct ?? 0}%`} ticks={[{ value: -50, label: '−50%' }, { value: -25, label: '−25%' }, { value: 0, label: '0%' }, { value: 30, label: '+30%' }]} onChange={(value) => previewScenario({ ...sc, revenue_change_pct: value })} onCommit={(value) => commitScenario({ ...sc, revenue_change_pct: value })} />
            <RangeField label="Изменение постоянных расходов" min={-20} max={50} step={5} value={sc.fixed_cost_change_pct ?? 0} displayValue={`${(sc.fixed_cost_change_pct ?? 0) > 0 ? '+' : ''}${sc.fixed_cost_change_pct ?? 0}%`} ticks={[{ value: -20, label: '−20%' }, { value: 0, label: '0%' }, { value: 25, label: '+25%' }, { value: 50, label: '+50%' }]} onChange={(value) => previewScenario({ ...sc, fixed_cost_change_pct: value })} onCommit={(value) => commitScenario({ ...sc, fixed_cost_change_pct: value })} />
            <RangeField label="Задержка оплаты клиентами" min={0} max={45} step={1} value={sc.delay_client_days ?? 0} displayValue={`${sc.delay_client_days ?? 0} дн.`} ticks={['0', '15 дней', '30 дней', '45 дней']} onChange={(value) => previewScenario({ ...sc, delay_client_days: value })} onCommit={(value) => commitScenario({ ...sc, delay_client_days: value })} />
            <label className="field" style={{ marginTop: 8 }}>Какой клиент<Select value={sc.delay_client_key ?? ''} onValueChange={(value) => commitScenario({ ...sc, delay_client_key: value || null })}><option value="">Все ожидаемые поступления от клиентов</option>{clients.slice(0, 10).map((c) => <option key={c.inn + c.name} value={c.name}>{c.name} ({fmtPct(c.share)})</option>)}</Select></label>
            <h3 style={{ marginTop: 12 }}>Внеплановый расход</h3>
            <div className="expense-fields"><input aria-label="Дата внепланового расхода" type="date" value={extra.date} onChange={(e) => setExtra({ ...extra, date: e.target.value })} /><input aria-label="Сумма внепланового расхода" type="number" min={1} placeholder="Сумма, ₽" value={extra.amount} onChange={(e) => setExtra({ ...extra, amount: e.target.value })} style={{ width: 120 }} /></div>
            <div className="expense-description"><input aria-label="Описание внепланового расхода" value={extra.label} onChange={(e) => setExtra({ ...extra, label: e.target.value })} style={{ flex: 1 }} /><button className="secondary" onClick={addExtra} aria-label="Добавить расход">+</button></div>
            {(sc.extra_expenses ?? []).map((e, i) => <div key={i} className="small muted">• {fmtDate(e.date)} — {e.label}: {fmtMoney(e.amount)} <button className="ghost" onClick={() => commitScenario({ ...sc, extra_expenses: sc.extra_expenses!.filter((_, j) => j !== i) })}>×</button></div>)}
            <button style={{ marginTop: 12, width: '100%' }} onClick={() => run(sc)} disabled={busy}>{busy ? 'Считаем…' : 'Пересчитать прогноз'}</button>
          </div>
        </div>
        <div className="grid scenario-results" aria-busy={busy}>
          <div className="forecast-status" role="status" aria-live="polite">{editing ? 'Отпустите ползунок, чтобы обновить прогноз' : busy ? 'Обновляем прогноз…' : error ? 'Не удалось обновить прогноз' : 'Прогноз обновлён'}</div>
          {error && <div className="card error" role="alert">{error}</div>}
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
          {!res && !error && <div className="empty">Считаем базовый сценарий…</div>}
        </div>
      </div>
    </>
  )
}
