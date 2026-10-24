import { chartTooltip } from '../chartTheme'
import useCompactChart from '../hooks/useCompactChart'
import Select from '../components/Select'
import ReactECharts from 'echarts-for-react'
import { useEffect, useState } from 'react'
import { get, patch, post } from '../api'
import { useAuth } from '../auth'
import { fmtDate, fmtMoney, fmtPct, fmtShort } from '../format'
import type { Category, Operation } from '../types'

const css = (n: string, f: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f
const SRC: Record<string, string> = { rule: 'правило', counterparty: 'по контрагенту', ml: 'ML', manual: 'вручную', fallback: 'не определено' }

export default function Operations() {
  const compact = useCompactChart()
  const { company, refreshCompanies } = useAuth()
  const [cats, setCats] = useState<Category[]>([])
  const [summary, setSummary] = useState<any>(null)
  const [ops, setOps] = useState<Operation[]>([])
  const [count, setCount] = useState(0)
  const [page, setPage] = useState(1)
  const [filters, setFilters] = useState({ search: '', category: '', direction: '' })
  const [months, setMonths] = useState(3)
  const [uploading, setUploading] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')

  useEffect(() => { get<Category[]>('/categories/').then(setCats) }, [])
  useEffect(() => { if (company) get(`/companies/${company.id}/categories/summary/`, { months }).then(setSummary) }, [company?.id, months])
  useEffect(() => {
    if (!company) return
    const params: any = { page, ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)) }
    get(`/companies/${company.id}/operations/`, params).then((r) => { setOps(r.results); setCount(r.count) })
  }, [company?.id, page, filters])

  const changeCategory = async (op: Operation, category: string) => {
    if (!company) return
    const updated = await patch<Operation>(`/companies/${company.id}/operations/${op.id}/`, { category })
    setOps((list) => list.map((o) => (o.id === op.id ? updated : o)))
  }

  const upload = async (file: File) => {
    if (!company) return
    setUploading(true); setUploadMsg('')
    try {
      const fd = new FormData(); fd.append('file', file)
      const r = await post(`/companies/${company.id}/upload/`, fd)
      setUploadMsg(`Загружено: строк ${r.report.rows_total}, новых ${r.report.rows_new ?? '—'}, дублей ${r.report.rows_duplicates}, пропусков ${r.report.gap_days}. ${r.report.warnings.join(' ')}`)
      await refreshCompanies(); setPage(1); setFilters({ ...filters })
    } catch (e: any) { setUploadMsg(e?.response?.data?.detail || 'Ошибка загрузки') } finally { setUploading(false) }
  }

  if (!company) return null
  const bar = (rows: any[], title: string) => {
    const data = rows.slice(0, 8)
    return (
      <div className="card">
        <h2>{title}</h2>
        {data.length === 0 ? <div className="empty">Нет данных</div> : (
          <ReactECharts notMerge style={{ height: 30 * data.length + 40 }} option={{
            grid: { left: 8, right: compact ? 52 : 70, top: 4, bottom: 4, containLabel: true },
            tooltip: { ...chartTooltip, formatter: (p: any) => `<b>${data[p.dataIndex].label}</b><br/>${fmtMoney(data[p.dataIndex].total)} за ${months} мес. · ${data[p.dataIndex].n} операций` },
            xAxis: { type: 'value', show: false },
            yAxis: { type: 'category', inverse: true, data: data.map((r) => r.label), axisLabel: { color: css('--text-2', '#52514e'), fontSize: 12, width: compact ? 90 : 190, overflow: 'truncate' }, axisLine: { show: false }, axisTick: { show: false } },
            series: [{ type: 'bar', barWidth: 14, data: data.map((r) => r.monthly_avg), itemStyle: { color: css('--series-1', '#2a78d6'), borderRadius: [0, 4, 4, 0] }, label: { show: true, position: 'right', color: css('--text-2', '#52514e'), fontSize: 11, formatter: (p: any) => compact ? fmtShort(p.value) : `${fmtShort(p.value)} / мес` } }],
          }} />
        )}
      </div>
    )
  }

  return (
    <>
      <div className="page-head">
        <div><h1>Операции и категории</h1><p className="subtitle">Каждая операция отнесена к категории правилами, по контрагенту или ML-моделью. Категорию можно поправить вручную — это учтётся в прогнозе.</p></div>
        <div className="row">
          <label className="btn secondary" style={{ cursor: 'pointer' }}>{uploading ? 'Загружаем…' : 'Загрузить выписку (CSV / 1С)'}<input type="file" accept=".csv,.txt" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} /></label>
        </div>
      </div>
      {uploadMsg && <div className="card tight small" style={{ marginBottom: 12 }}>{uploadMsg}</div>}

      <div className="toolbar">
        <label className="field">Период структуры<Select value={months} onValueChange={(value) => setMonths(Number(value))}><option value={1}>1 месяц</option><option value={3}>3 месяца</option><option value={6}>6 месяцев</option><option value={12}>12 месяцев</option></Select></label>
        {summary && <div className="small muted">Покрытие категоризации: <b>{fmtPct(summary.coverage)}</b> · источники: {Object.entries(summary.sources ?? {}).map(([k, v]) => `${SRC[k] ?? k} ${v}`).join(', ')}</div>}
      </div>
      {summary && <div className="grid grid-2" style={{ marginBottom: 16 }}>{bar(summary.inflow ?? [], 'Поступления по категориям (в среднем за месяц)')}{bar(summary.outflow ?? [], 'Списания по категориям (в среднем за месяц)')}</div>}

      <div className="card">
        <div className="toolbar">
          <input placeholder="Поиск по контрагенту, назначению, ИНН" value={filters.search} onChange={(e) => { setPage(1); setFilters({ ...filters, search: e.target.value }) }} style={{ width: 320 }} />
          <Select aria-label="Направление операции" value={filters.direction} onValueChange={(value) => { setPage(1); setFilters({ ...filters, direction: value }) }}><option value="">Все направления</option><option>Поступление</option><option>Списание</option></Select>
          <Select aria-label="Фильтр по категории" value={filters.category} onValueChange={(value) => { setPage(1); setFilters({ ...filters, category: value }) }}><option value="">Все категории</option>{cats.map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}</Select>
          <span className="muted small">найдено {count}</span>
        </div>
        <table>
          <thead><tr><th>Дата</th><th>Контрагент</th><th>Назначение</th><th className="num">Сумма</th><th>Категория</th><th className="num">Остаток</th></tr></thead>
          <tbody>
            {ops.map((o) => (
              <tr key={o.id}>
                <td style={{ whiteSpace: 'nowrap' }}>{fmtDate(o.operation_date)}</td>
                <td>{o.counterparty_name}<div className="small muted">{o.counterparty_inn || 'без ИНН'}</div></td>
                <td className="small" style={{ maxWidth: 380 }}>{o.payment_purpose || <span className="muted">— пустое назначение —</span>}</td>
                <td className={`num ${o.direction === 'Поступление' ? 'pos' : 'neg'}`}>{o.direction === 'Поступление' ? '+' : '−'}{fmtMoney(o.amount)}</td>
                <td>
                  <Select aria-label="Категория операции" value={o.category} onValueChange={(value) => changeCategory(o, value)} style={{ padding: '4px 6px', maxWidth: 220 }}>
                    {cats.filter((c) => c.direction === (o.direction === 'Поступление' ? 'in' : 'out')).map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}
                  </Select>
                  <div className="small muted">{SRC[o.category_source] ?? o.category_source} · {fmtPct(o.category_confidence)}</div>
                </td>
                <td className={`num ${Number(o.balance_eod) < 0 ? 'neg' : ''}`}>{fmtMoney(o.balance_eod)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 10 }}>
          <button className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Назад</button>
          <span className="muted small">стр. {page} из {Math.max(1, Math.ceil(count / 50))}</span>
          <button className="secondary" disabled={page * 50 >= count} onClick={() => setPage(page + 1)}>Вперёд →</button>
        </div>
      </div>
    </>
  )
}
