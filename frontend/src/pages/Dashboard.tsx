import Select from '../components/Select'
import { useEffect, useState, useRef } from 'react'
import { Link } from 'react-router-dom'
import { get, patch } from '../api'
import { useAuth } from '../auth'
import AlertList from '../components/AlertList'
import BalanceChart from '../components/BalanceChart'
import EventsTable from '../components/EventsTable'
import RiskCard from '../components/RiskCard'
import Waterfall from '../components/Waterfall'
import { addDays, fmtDate, fmtMoney, fmtShort } from '../format'
import type { ForecastResponse, HistoryPoint } from '../types'

export default function Dashboard() {
  const { company, refreshCompanies } = useAuth()
  const [data, setData] = useState<ForecastResponse | null>(null)
  const [summary, setSummary] = useState<any>(null)
  const [history, setHistory] = useState<HistoryPoint[]>([])
  const [actual, setActual] = useState<HistoryPoint[]>([])
  const [showActual, setShowActual] = useState(false)
  const [horizon, setHorizon] = useState(60)
  const [asOf, setAsOf] = useState<string>(company?.as_of ?? '')
  const [minBalance, setMinBalance] = useState<string>(String(Number(company?.min_balance) || 0))
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const requestVersion = useRef(0)
  useEffect(() => () => { requestVersion.current += 1 }, [])

  const load = async () => {
    if (!company) return
    const version = ++requestVersion.current
    setLoading(true); setErr('')
    try {
      const params: any = { horizon }
      if (asOf) params.as_of = asOf
      if (minBalance !== '') params.min_balance = Number(minBalance)
      const [fc, sm] = await Promise.all([get<ForecastResponse>(`/companies/${company.id}/forecast/`, params), get(`/companies/${company.id}/summary/`, asOf ? { as_of: asOf } : {})])
      const a = fc.forecast.as_of
      const [h, act] = await Promise.all([
        get<HistoryPoint[]>(`/companies/${company.id}/history/`, { from: addDays(a, -120), to: a }),
        get<HistoryPoint[]>(`/companies/${company.id}/history/`, { from: addDays(a, 1), to: addDays(a, horizon) }),
      ])
      if (version === requestVersion.current) { setData(fc); setSummary(sm); setHistory(h); setActual(act) }
    } catch (e: any) { if (version === requestVersion.current) setErr(e?.response?.data?.detail || 'Не удалось построить прогноз: проверьте дату и параметры') } finally { if (version === requestVersion.current) setLoading(false) }
  }
  useEffect(() => { if (company && asOf !== undefined) load() }, [company?.id, horizon])

  const saveSettings = async () => {
    if (!company) return
    try {
      await patch(`/companies/${company.id}/`, { as_of_override: asOf || null, min_balance: Number(minBalance) || 0 })
      await refreshCompanies(); await load()
    } catch { setErr('Не удалось сохранить параметры') }
  }

  if (!company) return null
  const fc = data?.forecast
  const risk = data?.risk
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{company.name}</h1>
          <p className="subtitle">{company.industry} · {company.legal_form} · {company.tax_regime || 'режим не определён'}{fc && <> · данные на {fmtDate(fc.as_of)}</>}</p>
        </div>
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <label className="field">Дата «сегодня»<input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
          <label className="field">Неснижаемый остаток<input type="number" step="10000" value={minBalance} onChange={(e) => setMinBalance(e.target.value)} style={{ width: 150 }} /></label>
          <label className="field">Горизонт
            <Select value={horizon} onValueChange={(value) => setHorizon(Number(value))}><option value={30}>30 дней</option><option value={60}>60 дней</option><option value={90}>90 дней</option></Select>
          </label>
          <button className="secondary" onClick={saveSettings} disabled={loading}>Применить</button>
        </div>
      </div>

      {err && <div className="card error">{err}</div>}
      {loading && !data && <div className="empty">Строим прогноз…</div>}

      {fc && risk && (
        <div className="grid">
          <div className="kpi-row">
            <div className="card stat"><span className="label">Остаток сегодня</span><span className="value">{fmtMoney(fc.opening_balance)}</span><span className="hint">на {fmtDate(fc.as_of)}</span></div>
            <div className="card stat"><span className="label">Через {horizon} дней (ожидаемо)</span><span className={`value ${fc.components.end_balance_p50 < fc.min_balance ? 'neg' : ''}`}>{fmtMoney(fc.components.end_balance_p50)}</span><span className="hint">минимум за период: {fmtShort(fc.components.min_balance_p50)}</span></div>
            <div className="card stat"><span className="label">Обязательные платежи впереди</span><span className="value">{fmtMoney(fc.components.det_out_total)}</span><span className="hint">зарплата, налоги, аренда, кредиты, счета</span></div>
            <div className="card stat"><span className="label">Ожидаемые поступления</span><span className="value">{fmtMoney(fc.components.stoch_in_total + fc.components.det_in_total)}</span><span className="hint">выручка по модели + известные счета</span></div>
          </div>

          <RiskCard risk={risk} asOf={fc.as_of} />
          <div className="small muted">{fc.components.probability_kind === 'calibrated' ? 'Вероятность откалибрована по истории: столько раз из ста в похожих ситуациях разрыв действительно случался.' : 'Вероятность — сырая оценка по симуляциям, а не гарантия.'} Проверка на исторических данных — в разделе «Качество прогноза».</div>
          <details className="card">
            <summary>Пошаговый журнал расчёта</summary>
            <p>Выгрузка содержит параметры, этапы вычислений, прогноз по дням и события текущей компании.</p>
            <button className="secondary" onClick={() => {
              const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
              const link = document.createElement('a'); link.href = url; link.download = `forecast-${company.code}-${fc.as_of}.json`; link.click(); URL.revokeObjectURL(url)
            }}>Скачать расчёт JSON</button>
            {fc.calculation_log?.map(step => <div key={step.step}><h3>{step.step}. {step.title}</h3><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(step, null, 2)}</pre></div>)}
          </details>

          <div className="card">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <h2>Остаток на счёте: факт и прогноз</h2>
              <div className="row">
                {actual.length > 0 && <label className="row small"><input type="checkbox" checked={showActual} onChange={(e) => setShowActual(e.target.checked)} /> показать, что было на самом деле</label>}
                <span className="badge info">модель: {fc.model}</span>
                <span className={`badge ${fc.data_quality.level === 'ok' ? 'good' : fc.data_quality.level === 'medium' ? 'medium' : 'high'}`}>данные: {fc.data_quality.level === 'ok' ? 'достаточно' : fc.data_quality.level === 'medium' ? 'меньше года' : 'мало'}</span>
              </div>
            </div>
            <BalanceChart daily={fc.daily} events={fc.events} history={history} actual={showActual ? actual : undefined} minBalance={fc.min_balance} />
          </div>

          <div className="grid grid-2">
            <div className="card">
              <h2>Предупреждения</h2>
              <AlertList alerts={risk.alerts} />
            </div>
            <div className="card">
              <h2>Что можно сделать</h2>
              {risk.recommendations.map((r, i) => (
                <div key={i} className="rec">
                  <div className="icon">{r.kind === 'credit' ? '🏦' : r.kind === 'collect' ? '📨' : r.kind === 'postpone' ? '📅' : r.kind === 'ok' ? '✓' : '🤝'}</div>
                  <div><div style={{ fontWeight: 600 }}>{r.title}</div><div className="muted">{r.text}</div></div>
                </div>
              ))}
              <div className="small muted" style={{ marginTop: 10 }}>Проверить сценарии — на странице <Link to="/scenarios" style={{ color: 'var(--series-1)' }}>«Что если»</Link>.</div>
            </div>
          </div>

          <div className="grid grid-2">
            <div className="card">
              <h2>Почему: от остатка сегодня до даты риска</h2>
              <Waterfall rows={risk.waterfall} />
            </div>
            <div className="card">
              <h2>Ближайшие платежи и поступления</h2>
              <EventsTable events={fc.events} limit={12} />
              <div className="small muted" style={{ marginTop: 8 }}>Полный список и добавление событий — в <Link to="/calendar" style={{ color: 'var(--series-1)' }}>календаре платежей</Link>.</div>
            </div>
          </div>

          {summary && (
            <div className="card tight small muted">
              История {summary.history_days} дн. · операций {summary.operations_total} · средний месячный оборот {fmtMoney(summary.monthly_turnover)} · {fc.data_quality.notes.join(' · ')}
              {summary.ingest_report?.gap_days ? ` · пропусков в выписке: ${summary.ingest_report.gap_days}` : ''}
            </div>
          )}
        </div>
      )}
    </>
  )
}
