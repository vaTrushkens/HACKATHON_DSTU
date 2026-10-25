import ReactECharts from 'echarts-for-react'
import { useEffect, useState } from 'react'
import { get, post } from '../api'
import { useAuth } from '../auth'
import { fmtMoney, fmtPct } from '../format'

const LABELS: Record<string, string> = { naive: 'Среднее 28 дней', seasonal_naive: 'Сезонная модель', hgb: 'Градиентный бустинг' }
const number = (n: number | null) => n == null ? '—' : n.toLocaleString('ru-RU', { maximumFractionDigits: 2 })

export default function Quality() {
  const { company } = useAuth()
  const [data, setData] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState('hgb')
  useEffect(() => {
    let active = true
    if (company) get('/backtest/', { company_id: company.id }).then(d => { if (active) setData(d) }).catch(() => { if (active) setError('Не удалось загрузить результаты') })
    return () => { active = false }
  }, [company?.id])
  const evaluate = async () => {
    if (!company) return
    setBusy(true); setError('')
    try { setData(await post('/backtest/', { company_id: company.id })) }
    catch (e: any) { setError(e?.response?.data?.detail || 'Не удалось выполнить проверку') }
    finally { setBusy(false) }
  }
  const models: any[] = data?.summary?.models ?? []
  const best = models.find(m => m.model === selected) ?? models[0]
  return <>
    <div className="page-head"><div><h1>Качество прогноза</h1><p className="subtitle">Проверка на истории компании «{company?.name}». Каждый прогноз использует только данные, доступные на дату его среза.</p></div>
      <button onClick={evaluate} disabled={busy || !company}>{busy ? 'Вычисляем исторические прогнозы…' : 'Проверить текущие данные'}</button></div>
    {error && <div className="card error">{error}</div>}
    {busy && <p role="status">Расчёт может занять несколько минут. Для проверки нужны 120 дней истории и 30 дней известного факта. Результат будет сохранён.</p>}
    {!data && !error && <div className="empty">Загрузка…</div>}
    {data?.detail && <div className="card">{data.detail}</div>}
    {data?.stale && <div className="card error">После проверки данные изменились. Запустите оценку заново.</div>}
    {data?.config?.warnings?.map((w: string) => <p key={w} className="muted">{w}</p>)}
    {data?.summary && !models.length && <div className="card">Недостаточно полного факта для выбранного горизонта. Метрики не вычислены.</div>}
    {best && <div className="grid">
      <div className="card">
        <p>{data.summary.n_forecasts} прогнозов; срезы {data.summary.cutoffs[0]} … {data.summary.cutoffs.slice(-1)[0]}; горизонт {data.summary.horizon} дней. Факт: {data.config.actual_mode === 'without_financing' ? 'остаток без финансирования после среза' : 'реальный остаток из выписки'}.</p>
        <p className="small muted">Accuracy, Precision, Recall и F1 относятся к обнаружению кассового разрыва в течение горизонта. Порог классификации — {fmtPct(data.config.gap_prob_threshold)}. Невычислимые показатели обозначены «—».</p>
        <div className="toolbar"><label className="field">Модель для подробного просмотра<select value={best.model} onChange={e => setSelected(e.target.value)}>{models.map(m => <option key={m.model} value={m.model}>{LABELS[m.model] ?? m.model}</option>)}</select></label>
          <button className="secondary" onClick={() => {
            const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
            const a = document.createElement('a'); a.href = url; a.download = `quality-${company?.code}.json`; a.click(); URL.revokeObjectURL(url)
          }}>Скачать метрики и параметры</button></div>
      </div>
      <div className="kpi-row">
        <div className="card stat"><span className="label">MAE остатка</span><span className="value">{fmtMoney(best.mae_balance)}</span><span className="hint">средняя абсолютная ошибка</span></div>
        <div className="card stat"><span className="label">Accuracy разрывов</span><span className="value">{fmtPct(best.gap_accuracy, 1)}</span><span className="hint">доля верных классификаций</span></div>
        <div className="card stat"><span className="label">Precision / Recall</span><span className="value">{fmtPct(best.gap_precision)} / {fmtPct(best.gap_recall)}</span><span className="hint">F1: {best.gap_f1 ?? '—'}</span></div>
        <div className="card stat"><span className="label">Покрытие интервала 10–90%</span><span className="value">{fmtPct(best.coverage80)}</span><span className="hint">номинальное покрытие — 80%</span></div>
      </div>
      <div className="card" style={{ overflowX: 'auto' }}><h2>Сравнение моделей</h2>
        <table><thead><tr>{['Модель', 'Прогнозов', 'MAE, ₽', 'MSE, ₽²', 'RMSE, ₽', 'MAE / оборот', 'MASE', 'К постоянному остатку', 'Accuracy', 'Precision', 'Recall', 'F1'].map(h => <th key={h}>{h}</th>)}</tr></thead>
          <tbody>{models.map(m => <tr key={m.model}><td>{LABELS[m.model] ?? m.model}</td><td>{m.n}</td><td>{number(m.mae_balance)}</td><td>{number(m.mse_balance)}</td><td>{number(m.rmse_balance)}</td><td>{fmtPct(m.mae_norm_median, 1)}</td><td>{number(m.mase_median)}</td><td>{number(m.naive_mae_ratio_median)}</td><td>{fmtPct(m.gap_accuracy, 1)}</td><td>{fmtPct(m.gap_precision)}</td><td>{fmtPct(m.gap_recall)}</td><td>{m.gap_f1 ?? '—'}</td></tr>)}</tbody></table>
        <p className="small muted">MASE нормирует ошибку на среднее абсолютное дневное изменение остатка в обучающей истории. Столбец «К постоянному остатку» сравнивает MAE с прогнозом неизменного остатка на том же горизонте: меньше 1 — лучше. MAE, MSE и RMSE усредняются по всем дням и срезам; нормированные ошибки представлены медианой по срезам.</p>
      </div>
      {best.by_horizon && <div className="card"><h2>Ошибка по горизонтам: {LABELS[best.model]}</h2>
        <table><thead><tr><th>Горизонт</th><th className="num">MAE / оборот</th><th className="num">К постоянному остатку</th></tr></thead>
          <tbody>{best.by_horizon.map((b: any) => <tr key={String(b.h)}><td>{b.h === 'full' ? `${data.summary.horizon} дней` : `${b.h} дней`}</td><td className="num">{fmtPct(b.mae_norm_median, 1)}</td><td className={`num ${b.naive_mae_ratio_median != null && b.naive_mae_ratio_median < 1 ? 'pos' : ''}`}>{number(b.naive_mae_ratio_median)}</td></tr>)}</tbody></table>
        <p className="small muted">Ближайшие дни важнее для решения о платежах: здесь видно, на каком горизонте модель выигрывает у гипотезы «остаток не изменится».</p>
      </div>}
      <div className="grid grid-2">
        <div className="card"><h2>Матрица ошибок: {LABELS[best.model]}</h2>
          <table><thead><tr><th>Факт / прогноз</th><th>Разрыв</th><th>Нет разрыва</th></tr></thead><tbody>
            <tr><th>Разрыв</th><td>TP: {best.confusion.tp}</td><td>FN: {best.confusion.fn}</td></tr>
            <tr><th>Нет разрыва</th><td>FP: {best.confusion.fp}</td><td>TN: {best.confusion.tn}</td></tr></tbody></table>
          <p>Доля фактических разрывов: {fmtPct(data.summary.actual_gap_rate)}. Упреждение верно обнаруженных разрывов: {best.lead_days_mean ?? '—'} дн. Ошибка даты: {best.date_err_days_mae ?? '—'} дн.</p>
        </div>
        <div className="card"><h2>Проверка вероятностей</h2><p className="small muted">{data.config.calibration ? `Вероятности откалиброваны по истории до ${data.config.calibration.test_end} (${data.config.calibration.samples} срезов).` : 'Вероятности — сырой результат симуляции, калибровка не обучена.'} Таблица показывает, насколько они согласуются с фактом. Brier score: {best.brier_score} (меньше — лучше).</p>
          <table><thead><tr><th>Вероятность</th><th>Доля разрывов</th><th>Случаев</th></tr></thead><tbody>{best.calibration.map((c: any) => <tr key={c.bin}><td>{c.bin}</td><td>{fmtPct(c.actual_rate)}</td><td>{c.n}</td></tr>)}</tbody></table>
        </div>
      </div>
      {best.early_warning && <div className="card"><h2>Заблаговременность: {LABELS[best.model]}</h2>
        <table><thead><tr><th>Сигнал</th><th className="num">Сработал раз</th><th className="num">Верен</th><th className="num">Разрывов обнаружено</th><th className="num">Упреждение, медиана</th><th className="num">≥ 7 дней</th><th className="num">≥ 14 дней</th></tr></thead>
          <tbody>{([['warning', 'Предупреждение (вероятность ≥ порога)'], ['early', 'Ранний сигнал (худшие 10 % траекторий)']] as [string, string][]).map(([k, title]) => { const w = best.early_warning[k]; return <tr key={k}><td>{title}</td><td className="num">{w.signals}</td><td className="num">{fmtPct(w.precision)}</td><td className="num">{w.detected} из {w.episodes}</td><td className="num">{w.lead_median == null ? '—' : `${w.lead_median} дн.`}</td><td className="num">{fmtPct(w.lead_ge7_share)}</td><td className="num">{fmtPct(w.lead_ge14_share)}</td></tr> })}</tbody></table>
        <p className="small muted">Упреждение — дни от самого раннего среза, на котором сигнал сработал, до первого дня разрыва; срезы, где остаток уже ниже порога, не учитываются. «Верен» — доля сигналов, после которых разрыв действительно случился в горизонте. Упреждение не может превышать горизонт прогноза и зависит от шага срезов.</p>
      </div>}
      <div className="card"><h2>Ошибка прогноза относительно оборота</h2><ReactECharts notMerge style={{ height: 220 }} option={{ tooltip: { trigger: 'axis' }, grid: { containLabel: true, left: 10, right: 25 }, xAxis: { type: 'category', data: models.map(m => LABELS[m.model]) }, yAxis: { type: 'value', name: '%' }, series: [{ type: 'bar', data: models.map(m => +(100 * m.mae_norm_median).toFixed(2)), itemStyle: { color: '#2a78d6' } }] }} /></div>
      <details className="card"><summary>Происхождение данных и параметры проверки</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(data.config, null, 2)}</pre></details>
      <p className="small muted">Оценка категорий операций требует отдельной ручной разметки. Результаты на ранее использованных для настройки 300 примерах не являются независимой оценкой контрольного набора.</p>
    </div>}
  </>
}
