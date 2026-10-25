import RangeField from '../components/RangeField'
import Select from '../components/Select'
import { useEffect, useState } from 'react'
import { del, get, patch, post, put } from '../api'
import { useAuth } from '../auth'
import { MODE_LABELS, PERIOD_LABELS, fmtDate, fmtMoney, fmtPct, sourceLabel } from '../format'
import type { Category, Contract, PlannedEvent, RecurringPattern } from '../types'

type Tab = 'recurring' | 'events' | 'contracts' | 'taxes' | 'integrations'

const emptyEvent = { kind: 'manual', direction: 'Поступление', amount: '', date: '', counterparty_name: '', label: '', probability: 1, category: 'REV_CLIENTS', note: '' }
const emptyContract = { number: '', counterparty_name: '', counterparty_inn: '', direction: 'Поступление', total_amount: '', start_date: '', end_date: '', schedule_type: 'prepay_final', prepayment_pct: 30, deferral_days: 0, installments: 3, day_of_month: '', category: 'REV_CLIENTS', note: '' }

export default function Calendar() {
  const { company } = useAuth()
  const [tab, setTab] = useState<Tab>('recurring')
  const [patterns, setPatterns] = useState<RecurringPattern[]>([])
  const [events, setEvents] = useState<PlannedEvent[]>([])
  const [taxes, setTaxes] = useState<any>(null)
  const [integrations, setIntegrations] = useState<any[]>([])
  const [cats, setCats] = useState<Category[]>([])
  const [form, setForm] = useState<any>(emptyEvent)
  const [contracts, setContracts] = useState<Contract[]>([])
  const [cform, setCform] = useState<any>(emptyContract)
  const [msg, setMsg] = useState('')
  const [cfg, setCfg] = useState<Record<string, Record<string, string>>>({})
  const [imp, setImp] = useState({ direction: 'auto', deferral_days: 14, fee_pct: 2 })
  const [busySource, setBusySource] = useState('')

  const loadAll = async () => {
    if (!company) return
    const [p, e, t, i, c] = await Promise.all([
      get<RecurringPattern[]>(`/companies/${company.id}/recurring/`),
      get<PlannedEvent[]>(`/companies/${company.id}/events/`),
      get(`/companies/${company.id}/tax-calendar/`),
      get(`/companies/${company.id}/integrations/`),
      get<Contract[]>(`/companies/${company.id}/contracts/`),
    ])
    setPatterns(p); setEvents(e); setTaxes(t); setIntegrations(i); setContracts(c)
  }
  const addContract = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      const body: any = { ...cform, total_amount: Number(cform.total_amount), end_date: cform.end_date || null, day_of_month: cform.day_of_month ? Number(cform.day_of_month) : null }
      const r = await post<Contract>(`/companies/${company!.id}/contracts/`, body)
      setCform(emptyContract); setMsg(`Договор добавлен: ${r.events.length} ожидаемых платежей попали в прогноз`); loadAll()
    } catch (err: any) { setMsg(JSON.stringify(err?.response?.data ?? 'Ошибка')) }
  }
  const removeContract = async (id: number) => { await del(`/companies/${company!.id}/contracts/${id}/`); loadAll() }
  useEffect(() => { loadAll(); get<Category[]>('/categories/').then(setCats) }, [company?.id])

  if (!company) return null

  const togglePattern = async (p: RecurringPattern) => {
    const u = await patch<RecurringPattern>(`/companies/${company.id}/recurring/${p.id}/`, { user_disabled: !p.user_disabled })
    setPatterns((l) => l.map((x) => (x.id === p.id ? u : x)))
  }
  const addEvent = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await post(`/companies/${company.id}/events/`, { ...form, amount: Number(form.amount), source: 'manual' })
      setForm(emptyEvent); setMsg('Событие добавлено — прогноз учтёт его при следующем расчёте'); loadAll()
    } catch (err: any) { setMsg(JSON.stringify(err?.response?.data ?? 'Ошибка')) }
  }
  const cancelEvent = async (id: number) => { await del(`/companies/${company.id}/events/${id}/`); loadAll() }
  const fail = (err: any) => setMsg(err?.response?.data?.detail || JSON.stringify(err?.response?.data ?? 'Ошибка'))
  const sync = async (source: string) => {
    setBusySource(source)
    try {
      const r = await post(`/companies/${company.id}/integrations/`, { source })
      const mode = r.mode === 'api' ? 'через API' : r.mode === 'derived' ? 'восстановлено из выписки' : r.mode === 'mock' ? 'по заглушке' : ''
      setMsg(source === 'registry' ? `Проверка реестра ${mode}: вероятность оплаты снижена у ${r.updated_invoices} счетов` : `Синхронизация «${sourceLabel(source)}» ${mode}: событий в прогнозе ${r.events}${r.total ? `, ожидается ${fmtMoney(r.total)}` : ''}`)
      loadAll()
    } catch (err) { fail(err) } finally { setBusySource('') }
  }
  const saveConfig = async (kind: string) => {
    try { const r = await put(`/companies/${company.id}/integrations/${kind}/config/`, { config: cfg[kind] ?? {} }); setMsg(r.configured ? 'Ключи сохранены — можно синхронизировать' : 'Ключи сохранены, но для подключения заполнены не все поля'); setCfg({ ...cfg, [kind]: {} }); loadAll() } catch (err) { fail(err) }
  }
  const clearConfig = async (kind: string) => { await del(`/companies/${company.id}/integrations/${kind}/config/`); setMsg('Ключи удалены'); loadAll() }
  const testKeys = async (source: string) => {
    setBusySource(source)
    try { const r = await post(`/companies/${company.id}/integrations/`, { source, action: 'test' }); setMsg(`${r.ok ? '✓' : '✗'} ${sourceLabel(source)}: ${r.detail}`) } catch (err) { fail(err) } finally { setBusySource('') }
  }
  const importFile = async (source: string, action: string, file: File) => {
    const fd = new FormData(); fd.append('file', file)
    if (action === 'import_invoices') { fd.append('direction', imp.direction); fd.append('deferral_days', String(imp.deferral_days)) } else fd.append('fee_pct', String(imp.fee_pct))
    setBusySource(source)
    try {
      const r = await post(`/companies/${company.id}/import/${action === 'import_invoices' ? 'invoices' : 'ofd'}/`, fd)
      setMsg(action === 'import_invoices'
        ? `Импорт (${sourceLabel(r.source)}): документов ${r.rows_total}, добавлено в прогноз ${r.created} (поступлений ${r.incoming}, списаний ${r.outgoing}), уже были ${r.duplicates}${r.direction_default ? `, у ${r.direction_default} направление не определено — принято «поступление»` : ''}${r.skipped ? `, пропущено без даты/суммы ${r.skipped}` : ''}`
        : `Отчёт ОФД: дней ${r.days}, безнал ${fmtMoney(r.card_total)}, наличные ${fmtMoney(r.cash_total)} (на счёт не попадают) → зачислений в прогноз ${r.created}, уже в выписке ${r.already_in_statement}, повторов ${r.duplicates}`)
      loadAll()
    } catch (err) { fail(err) } finally { setBusySource('') }
  }
  const refreshStatuses = async () => { const r = await post(`/companies/${company.id}/events/refresh-status/`); setMsg(`Сверка с фактом: исполнено ${r.matched}, просрочено ${r.overdue}`); loadAll() }

  const active = patterns.filter((p) => p.active)
  return (
    <>
      <div className="page-head"><div><h1>Календарь платежей</h1><p className="subtitle">Всё, что сервис ожидает в будущем: выявленные регулярные платежи, налоги по календарю, счета и события, введённые вручную. У каждого события виден источник.</p></div></div>
      <div className="tabs">
        {([['recurring', `Регулярные (${active.length})`], ['events', `Счета и события (${events.filter((e) => e.status !== 'cancelled').length})`], ['contracts', `Договоры (${contracts.length})`], ['taxes', 'Налоги'], ['integrations', 'Источники данных']] as [Tab, string][]).map(([k, l]) => <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      {msg && <div className="card tight small" style={{ marginBottom: 12 }}>{msg}</div>}

      {tab === 'recurring' && (
        <div className="card">
          <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}><h2>Регулярные платежи и поступления, найденные в выписке</h2><button className="secondary" onClick={async () => { await post(`/companies/${company.id}/recurring/`); loadAll() }}>Пересчитать</button></div>
          <table>
            <thead><tr><th>Платёж</th><th>Периодичность</th><th>День</th><th className="num">Сумма</th><th className="num">Разброс</th><th className="num">Повторов</th><th>Последний</th><th className="num">Вероятность</th><th className="num">Уверенность</th><th>Учитывать</th></tr></thead>
            <tbody>
              {patterns.map((p) => (
                <tr key={p.id} style={{ opacity: p.active && !p.user_disabled ? 1 : 0.5 }}>
                  <td>{p.label}<div className="small muted">{p.category_label}</div></td>
                  <td>{PERIOD_LABELS[p.period] ?? p.period}</td>
                  <td>{p.day_of_month ? (p.day_of_month >= 31 ? 'конец месяца' : `${p.day_of_month}-е`) : p.weekday !== null ? ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][p.weekday] : '—'}{p.timing_std_days > 0 && <div className="small muted">±{Math.round(p.timing_std_days)} дн.</div>}</td>
                  <td className={`num ${p.direction === 'Поступление' ? 'pos' : 'neg'}`}>{fmtMoney(p.amount)}</td>
                  <td className="num muted">±{fmtPct(p.amount_cv)}</td>
                  <td className="num">{p.occurrences}</td>
                  <td>{fmtDate(p.last_date)}{!p.active && <div className="small muted">не повторялся — неактивен</div>}</td>
                  <td className="num">{fmtPct(p.probability)}{p.occurrences < 3 && <div className="small muted">мало повторов</div>}</td>
                  <td className="num"><div className="progress" style={{ width: 60, display: 'inline-block' }}><i style={{ width: `${p.confidence * 100}%` }} /></div></td>
                  <td><input type="checkbox" checked={!p.user_disabled} onChange={() => togglePattern(p)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'events' && (
        <div className="grid calendar-layout">
          <div className="card">
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}><h2>Ожидаемые события</h2><button className="secondary" onClick={refreshStatuses}>Сверить с фактом</button></div>
            <table>
              <thead><tr><th>Дата</th><th>Событие</th><th>Тип / источник</th><th className="num">Сумма</th><th className="num">Вероятность</th><th>Статус</th><th /></tr></thead>
              <tbody>
                {events.filter((e) => e.status !== 'cancelled').map((e) => (
                  <tr key={e.id}>
                    <td>{fmtDate(e.date)}{e.due_date && <div className="small muted">срок {fmtDate(e.due_date)}</div>}</td>
                    <td>{e.label}<div className="small muted">{e.counterparty_name}</div></td>
                    <td><div>{e.kind_label}</div><span className="source-tag">{sourceLabel(e.source)}</span></td>
                    <td className={`num ${e.direction === 'Поступление' ? 'pos' : 'neg'}`}>{e.direction === 'Поступление' ? '+' : '−'}{fmtMoney(e.amount)}</td>
                    <td className="num">{fmtPct(e.probability)}{e.timing_std_days > 0 && <div className="small muted">±{Math.round(e.timing_std_days)} дн.</div>}{e.meta?.registry && <div className="small muted">реестр: {e.meta.registry.status}</div>}</td>
                    <td><span className={`badge ${e.status === 'overdue' ? 'high' : e.status === 'matched' ? 'good' : 'info'}`}>{e.status === 'expected' ? 'ожидается' : e.status === 'matched' ? `исполнено ${e.matched_date ? fmtDate(e.matched_date) : ''}` : e.status === 'overdue' ? 'просрочено' : e.status}</span></td>
                    <td>{e.status === 'expected' && <button className="ghost" onClick={() => cancelEvent(e.id)}>отменить</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {events.length === 0 && <div className="empty">Пока нет ожидаемых событий — добавьте первое справа</div>}
          </div>
          <form className="card" onSubmit={addEvent}>
            <h2>Добавить событие</h2>
            <p className="small muted">Например: «клиент обещал заплатить 300 000 ₽ 20-го» или «в декабре куплю оборудование за 1 млн».</p>
            <div className="grid" style={{ gap: 10 }}>
              <label className="field">Тип<Select value={form.kind} onValueChange={(value) => setForm({ ...form, kind: value })}><option value="manual">Ручной ввод</option><option value="invoice_in">Выставленный счёт клиенту</option><option value="invoice_out">Счёт от поставщика</option><option value="contract">Платёж по договору</option></Select></label>
              <label className="field">Направление<Select value={form.direction} onValueChange={(value) => { const d = value; setForm({ ...form, direction: d, category: d === 'Поступление' ? 'REV_CLIENTS' : 'SUPPLIERS' }) }}><option>Поступление</option><option>Списание</option></Select></label>
              <label className="field">Сумма, ₽<input type="number" required min={1} value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} /></label>
              <label className="field">Ожидаемая дата<input type="date" required value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value })} /></label>
              <label className="field">Описание<input required value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="Оплата по счёту № 15" /></label>
              <label className="field">Контрагент<input value={form.counterparty_name} onChange={(e) => setForm({ ...form, counterparty_name: e.target.value })} /></label>
              <label className="field">Категория<Select value={form.category} onValueChange={(value) => setForm({ ...form, category: value })}>{cats.filter((c) => c.direction === (form.direction === 'Поступление' ? 'in' : 'out')).map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}</Select></label>
              <RangeField label="Вероятность события" min={0.1} max={1} step={0.05} value={form.probability} displayValue={fmtPct(form.probability)} ticks={['10%', '40%', '70%', '100%']} onChange={(value) => setForm({ ...form, probability: value })} />
              <button>Добавить в прогноз</button>
            </div>
          </form>
        </div>
      )}

      {tab === 'contracts' && (
        <div className="grid calendar-layout">
          <div className="card">
            <h2>Договоры и их графики платежей</h2>
            {contracts.length === 0 && <div className="empty">Договоров пока нет — добавьте первый справа. Условия (предоплата, отсрочка, рассрочка) превратятся в ожидаемые платежи.</div>}
            {contracts.map((c) => (
              <div key={c.id} style={{ padding: '10px 0', borderTop: '1px solid var(--grid)' }}>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <div><b>{c.number ? `Договор ${c.number}` : 'Договор'}</b> · {c.counterparty_name} <span className="muted small">· {c.schedule_label}{c.deferral_days ? `, отсрочка ${c.deferral_days} дн.` : ''}</span></div>
                  <div className="row"><span className={c.direction === 'Поступление' ? 'pos' : 'neg'}>{c.direction === 'Поступление' ? '+' : '−'}{fmtMoney(c.total_amount)}</span><button className="ghost" onClick={() => removeContract(c.id)}>расторгнуть</button></div>
                </div>
                <table style={{ marginTop: 6 }}>
                  <tbody>{c.events.map((e) => <tr key={e.id}><td style={{ width: 130 }}>{fmtDate(e.date)}</td><td>{e.label}</td><td className="num">{fmtMoney(e.amount)}</td><td className="num muted small">{fmtPct(e.probability)}{e.timing_std_days > 0 ? ` · ±${Math.round(e.timing_std_days)} дн.` : ''}</td><td><span className={`badge ${e.status === 'overdue' ? 'high' : e.status === 'matched' ? 'good' : 'info'}`}>{e.status === 'expected' ? 'ожидается' : e.status === 'matched' ? 'исполнено' : e.status === 'overdue' ? 'просрочено' : e.status}</span></td></tr>)}</tbody>
                </table>
              </div>
            ))}
          </div>
          <form className="card" onSubmit={addContract}>
            <h2>Добавить договор</h2>
            <p className="small muted">Для клиентов ожидаемая дата сдвигается на его типичную задержку оплаты, а разброс даты берётся из истории.</p>
            <div className="grid" style={{ gap: 10 }}>
              <label className="field">С кем<Select value={cform.direction} onValueChange={(value) => { const d = value; setCform({ ...cform, direction: d, category: d === 'Поступление' ? 'REV_CLIENTS' : 'SUPPLIERS' }) }}><option value="Поступление">Клиент (нам платят)</option><option value="Списание">Поставщик / подрядчик (мы платим)</option></Select></label>
              <label className="field">Контрагент<input required value={cform.counterparty_name} onChange={(e) => setCform({ ...cform, counterparty_name: e.target.value })} /></label>
              <div className="row"><label className="field" style={{ flex: 1 }}>ИНН<input value={cform.counterparty_inn} onChange={(e) => setCform({ ...cform, counterparty_inn: e.target.value })} /></label><label className="field" style={{ flex: 1 }}>№ договора<input value={cform.number} onChange={(e) => setCform({ ...cform, number: e.target.value })} /></label></div>
              <label className="field">Сумма договора, ₽<input type="number" required min={1} value={cform.total_amount} onChange={(e) => setCform({ ...cform, total_amount: e.target.value })} /></label>
              <label className="field">График<Select value={cform.schedule_type} onValueChange={(value) => setCform({ ...cform, schedule_type: value })}><option value="prepay_final">Предоплата + окончательный расчёт</option><option value="postpay">Оплата после закрытия с отсрочкой</option><option value="equal">Равные платежи ежемесячно</option></Select></label>
              <div className="row"><label className="field" style={{ flex: 1 }}>Начало<input type="date" required value={cform.start_date} onChange={(e) => setCform({ ...cform, start_date: e.target.value })} /></label><label className="field" style={{ flex: 1 }}>Закрытие (акт / поставка)<input type="date" value={cform.end_date} onChange={(e) => setCform({ ...cform, end_date: e.target.value })} /></label></div>
              {cform.schedule_type === 'prepay_final' && <RangeField label="Предоплата по договору" min={0} max={100} step={5} value={cform.prepayment_pct} displayValue={`${cform.prepayment_pct}%`} ticks={['0%', '25%', '50%', '75%', '100%']} onChange={(value) => setCform({ ...cform, prepayment_pct: value })} />}
              {cform.schedule_type === 'equal' && <div className="row"><label className="field" style={{ flex: 1 }}>Число платежей<input type="number" min={1} max={36} value={cform.installments} onChange={(e) => setCform({ ...cform, installments: Number(e.target.value) })} /></label><label className="field" style={{ flex: 1 }}>День месяца<input type="number" min={1} max={31} value={cform.day_of_month} onChange={(e) => setCform({ ...cform, day_of_month: e.target.value })} /></label></div>}
              <label className="field">Отсрочка оплаты, дн.<input type="number" min={0} max={180} value={cform.deferral_days} onChange={(e) => setCform({ ...cform, deferral_days: Number(e.target.value) })} /></label>
              <button>Добавить в прогноз</button>
            </div>
          </form>
        </div>
      )}

      {tab === 'taxes' && taxes && (
        <div className="grid grid-2">
          <div className="card">
            <h2>Предстоящие налоговые платежи</h2>
            <p className="small muted">Режим: <b>{taxes.regime?.regime}</b> · доля поступлений с НДС {fmtPct(taxes.regime?.vat_share)} · налоговая нагрузка {fmtPct(taxes.regime?.tax_burden, 1)} от выручки. ЕНП — до 28-го числа (перенос на рабочий день), квартальные авансы учтены по истории прошлого года.</p>
            <table><thead><tr><th>Срок</th><th>Платёж</th><th className="num">Оценка</th><th className="num">Уверенность</th></tr></thead>
              <tbody>{taxes.events.map((t: any, i: number) => <tr key={i}><td>{fmtDate(t.date)}</td><td>{t.label}</td><td className="num neg">−{fmtMoney(t.amount)}</td><td className="num muted">{fmtPct(t.confidence)}</td></tr>)}</tbody></table>
            {taxes.events.length === 0 && <div className="empty">Налоговых платежей в истории нет или они уже уплачены в этом периоде</div>}
          </div>
          <div className="card"><h2>История ЕНП по месяцам</h2>
            <table><thead><tr><th>Месяц</th><th className="num">Уплачено</th></tr></thead><tbody>{(taxes.history ?? []).slice(-12).map((h: any) => <tr key={h.month}><td>{h.month}</td><td className="num">{fmtMoney(h.amount)}</td></tr>)}</tbody></table></div>
        </div>
      )}

      {tab === 'integrations' && (
        <>
          <div className="card tight small muted" style={{ marginBottom: 12 }}>
            Ключи API хранятся по компании и наружу не показываются. Файлы: 1С — CommerceML или таблица счетов (CSV/XLSX), ЭДО — УПД XML формата ФНС, ОФД — отчёт по сменам или чекам. Примеры в <code>data/samples/</code>.
          </div>
          <div className="grid grid-2">
            {integrations.map((s) => {
              const [modeLabel, modeClass] = MODE_LABELS[s.mode] ?? [s.mode, 'info']
              const kindLabel = s.kind === 'internal' ? 'внутренняя' : s.kind === 'external' ? 'внешняя' : 'ручная'
              return (
                <div key={s.source} className="card">
                  <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>{s.title}</h2><span className={`badge ${modeClass}`}>{kindLabel} · {modeLabel}</span></div>
                  <p className="muted">{s.description}</p>
                  {s.fields && (
                    <form className="source-config" onSubmit={(e) => { e.preventDefault(); saveConfig(s.config) }}>
                      {s.fields.map((f: any) => (
                        <label key={f.name} className="field">{f.label}
                          <input type={f.name === 'base_url' ? 'url' : 'password'} autoComplete="off" value={cfg[s.config]?.[f.name] ?? ''} placeholder={s.values?.[f.name] || 'не задано'} onChange={(e) => setCfg({ ...cfg, [s.config]: { ...(cfg[s.config] ?? {}), [f.name]: e.target.value } })} />
                        </label>
                      ))}
                      <div className="row">
                        <button className="secondary" type="submit">Сохранить ключи</button>
                        {s.configured && <button type="button" className="secondary" disabled={busySource === s.source} onClick={() => testKeys(s.source)}>Проверить ключ</button>}
                        {s.configured && <button type="button" className="ghost" onClick={() => clearConfig(s.config)}>Удалить</button>}
                        {s.last_sync && <span className={`small ${s.last_status === 'error' ? 'error' : 'muted'}`} title={s.last_error}>{s.last_status === 'error' ? 'ошибка синхронизации' : 'синхронизировано'} {fmtDate(s.last_sync)}</span>}
                      </div>
                      {s.last_status === 'error' && s.last_error && <div className="small error">{s.last_error}</div>}
                    </form>
                  )}
                  {s.actions.includes('import_invoices') && (
                    <div className="row source-import">
                      <label className="field">Чьи документы
                        <Select value={imp.direction} onValueChange={(value) => setImp({ ...imp, direction: value })}><option value="auto">Определить по ИНН / роли</option><option value="in">Выставлены нами — нам заплатят</option><option value="out">Получены от поставщиков — платим мы</option></Select>
                      </label>
                      <label className="field">Отсрочка, дн.<input type="number" min={0} max={180} value={imp.deferral_days} onChange={(e) => setImp({ ...imp, deferral_days: Number(e.target.value) })} style={{ width: 110 }} /></label>
                      <label className="btn secondary" style={{ cursor: 'pointer' }}>{busySource === s.source ? 'Загружаем…' : 'Импорт файла'}<input type="file" accept=".xml,.csv,.xlsx,.xls,.txt" hidden onChange={(e) => e.target.files?.[0] && importFile(s.source, 'import_invoices', e.target.files[0])} /></label>
                    </div>
                  )}
                  {s.actions.includes('import_ofd') && (
                    <div className="row source-import">
                      <label className="field">Комиссия эквайринга, %<input type="number" min={0} max={10} step={0.1} value={imp.fee_pct} onChange={(e) => setImp({ ...imp, fee_pct: Number(e.target.value) })} style={{ width: 110 }} /></label>
                      <label className="btn secondary" style={{ cursor: 'pointer' }}>{busySource === s.source ? 'Загружаем…' : 'Импорт отчёта'}<input type="file" accept=".csv,.xlsx,.xls,.txt" hidden onChange={(e) => e.target.files?.[0] && importFile(s.source, 'import_ofd', e.target.files[0])} /></label>
                    </div>
                  )}
                  <div className="row" style={{ marginTop: 10 }}>
                    <span className="small muted">событий в прогнозе: {s.events}</span>
                    {s.actions.includes('sync') && <button className="secondary" disabled={busySource === s.source} onClick={() => sync(s.source)}>{busySource === s.source ? 'Синхронизируем…' : s.source === 'registry' ? 'Проверить контрагентов' : 'Синхронизировать'}</button>}
                  </div>
                </div>
              )
            })}
          </div>
        </>
      )}
    </>
  )
}
