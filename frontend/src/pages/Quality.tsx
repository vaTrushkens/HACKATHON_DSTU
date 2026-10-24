import useCompactChart from '../hooks/useCompactChart'
import ReactECharts from 'echarts-for-react'
import { useEffect, useState } from 'react'
import { get } from '../api'
import { fmtPct } from '../format'

const css = (n: string, f: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f
const MODEL_LABELS: Record<string, string> = { naive: 'Наивная (среднее 28 дн.)', seasonal_naive: 'Сезонная наивная (день недели × месяц)', hgb: 'Градиентный бустинг (глобальная модель)' }

export default function Quality() {
  const compact = useCompactChart()
  const [data, setData] = useState<any>(null)
  useEffect(() => { get('/backtest/').then(setData) }, [])
  if (!data) return <div className="empty">Загрузка…</div>
  if (data.detail) return <><h1>Качество прогноза</h1><div className="card">{data.detail}</div></>
  const models: any[] = data.summary.models
  const ink = css('--text-2', '#52514e'), grid = css('--grid', '#e1e0d9'), muted = css('--muted', '#898781'), blue = css('--series-1', '#2a78d6')
  const barOpt = (key: string, title: string, fmt: (v: number) => string) => ({
    title: { text: compact ? title.split(' (')[0] : title, left: 0, textStyle: { fontSize: compact ? 11 : 13, color: ink, fontWeight: 600 } },
    grid: { left: 8, right: compact ? 42 : 60, top: 32, bottom: 8, containLabel: true },
    xAxis: { type: 'value', show: false },
    yAxis: { type: 'category', inverse: true, data: models.map((m) => MODEL_LABELS[m.model] ?? m.model), axisLabel: { color: ink, fontSize: 12, width: compact ? 95 : 200, overflow: 'truncate' }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{ type: 'bar', barWidth: 16, data: models.map((m) => m[key]), itemStyle: { color: blue, borderRadius: [0, 4, 4, 0] }, label: { show: true, position: 'right', color: ink, fontSize: 11, formatter: (p: any) => (p.value === null ? '—' : fmt(p.value)) } }],
  })
  const best = models.find((m) => m.model === 'hgb') ?? models[0]
  return (
    <>
      <div className="page-head"><div><h1>Качество прогноза</h1><p className="subtitle">Rolling-origin бэктест: {data.summary.n_forecasts} прогнозов по {data.summary.n_companies} компаниям, срезы {data.summary.cutoffs?.[0]} … {data.summary.cutoffs?.slice(-1)[0]}, горизонт {data.summary.horizon} дней. Факт сравнивается без «спасательных» вливаний собственника и кредитов.</p></div></div>
      <div className="grid">
        <div className="kpi-row">
          <div className="card stat"><span className="label">Ошибка остатка (медиана, % от месячного оборота)</span><span className="value">{fmtPct(best.mae_norm_median, 1)}</span><span className="hint">{MODEL_LABELS[best.model]}</span></div>
          <div className="card stat"><span className="label">MASE к «остаток не меняется»</span><span className="value">{best.mase_median}</span><span className="hint">меньше 1 — лучше наивного</span></div>
          <div className="card stat"><span className="label">Покрытие коридора 10–90%</span><span className="value">{fmtPct(best.coverage80)}</span><span className="hint">цель ≈ 80%</span></div>
          <div className="card stat"><span className="label">Разрывы: точность / полнота</span><span className="value">{fmtPct(best.gap_precision)} / {fmtPct(best.gap_recall)}</span><span className="hint">упреждение в среднем {best.lead_days_mean ?? '—'} дн.</span></div>
        </div>
        <div className="grid grid-2">
          <div className="card"><ReactECharts notMerge style={{ height: 180 }} option={barOpt('mae_norm_median', 'Ошибка остатка, % от оборота (меньше — лучше)', (v) => fmtPct(v, 1))} /></div>
          <div className="card"><ReactECharts notMerge style={{ height: 180 }} option={barOpt('gap_f1', 'F1 детекции кассовых разрывов (больше — лучше)', (v) => v.toFixed(2))} /></div>
        </div>
        <div className="card">
          <h2>Сравнение подходов</h2>
          <table>
            <thead><tr><th>Модель</th><th className="num">Прогнозов</th><th className="num">MAE / оборот</th><th className="num">MASE</th><th className="num">Покрытие 80%</th><th className="num">Ошибка на конец горизонта</th><th className="num">Precision</th><th className="num">Recall</th><th className="num">F1</th><th className="num">Упреждение, дн.</th><th className="num">Ошибка даты, дн.</th></tr></thead>
            <tbody>{models.map((m) => <tr key={m.model}><td>{MODEL_LABELS[m.model] ?? m.model}</td><td className="num">{m.n}</td><td className="num">{fmtPct(m.mae_norm_median, 1)}</td><td className="num">{m.mase_median}</td><td className="num">{fmtPct(m.coverage80)}</td><td className="num">{fmtPct(m.end_err_norm_median, 1)}</td><td className="num">{fmtPct(m.gap_precision)}</td><td className="num">{fmtPct(m.gap_recall)}</td><td className="num">{m.gap_f1 ?? '—'}</td><td className="num">{m.lead_days_mean ?? '—'}</td><td className="num">{m.date_err_days_mae ?? '—'}</td></tr>)}</tbody>
          </table>
          <p className="small muted">Реальных разрывов в тестовых срезах: {fmtPct(data.summary.actual_gap_rate)} случаев. Матрица для {MODEL_LABELS[best.model]}: TP {best.confusion.tp}, FP {best.confusion.fp}, FN {best.confusion.fn}, TN {best.confusion.tn}.</p>
        </div>
        <div className="grid grid-2">
          <div className="card">
            <h2>Калибровка вероятности разрыва</h2>
            <p className="small muted">Если сервис говорит «60–80%», разрыв должен случаться примерно в 70% таких случаев.</p>
            <table><thead><tr><th>Предсказанная вероятность</th><th className="num">Фактическая доля</th><th className="num">Случаев</th></tr></thead><tbody>{best.calibration.map((c: any) => <tr key={c.bin}><td>{c.bin}</td><td className="num">{fmtPct(c.actual_rate)}</td><td className="num">{c.n}</td></tr>)}</tbody></table>
          </div>
          <div className="card">
            <h2>Категоризация операций</h2>
            {data.categorizer_metrics?.manual ? (
              <>
                <p className="small muted">Честная оценка: {data.categorizer_metrics.manual.n} операций размечены вручную. {data.categorizer_metrics.manual.sampling}.</p>
                <table>
                  <thead><tr><th>Вариант</th><th className="num">Accuracy</th><th className="num">Macro-F1</th><th className="num">Ошибок</th></tr></thead>
                  <tbody>
                    {[['rules_only', 'Только правила'], ['rules_counterparty', 'Правила + контрагент'], ['full_pipeline', 'Правила + контрагент + ML']].map(([k, l]) => { const m = data.categorizer_metrics.manual[k]; return m ? <tr key={k}><td>{l}</td><td className="num"><b>{fmtPct(m.accuracy, 1)}</b></td><td className="num">{m.macro_f1}</td><td className="num">{m.errors}</td></tr> : null })}
                  </tbody>
                </table>
                {data.categorizer_metrics.manual.error_examples?.length > 0 && (
                  <details style={{ marginTop: 8 }}>
                    <summary className="small muted" style={{ cursor: 'pointer' }}>Примеры ошибок полного пайплайна ({data.categorizer_metrics.manual.full_pipeline?.errors})</summary>
                    <table className="small"><tbody>{data.categorizer_metrics.manual.error_examples.map((e: any, i: number) => <tr key={i}><td>{e.counterparty}</td><td>{e.purpose}</td><td>{e.label} → <span className="neg">{e.predicted}</span></td></tr>)}</tbody></table>
                  </details>
                )}
              </>
            ) : <p className="small muted">Ручная разметка не выполнена (manage.py evaluate_categorizer).</p>}
            <p className="small muted" style={{ marginTop: 10 }}>Согласие ML с правилами на отложенной выборке (не является честной метрикой, показывает лишь, что модель воспроизводит правила):</p>
            {Object.entries(data.categorizer_metrics ?? {}).filter(([k]) => k !== 'manual').map(([dir, m]: any) => <div key={dir} className="row small" style={{ justifyContent: 'space-between', padding: '4px 0', borderTop: `1px solid ${grid}` }}><span>{dir === 'in' ? 'Поступления' : 'Списания'}</span><span>accuracy {fmtPct(m.accuracy, 1)} · macro-F1 {m.macro_f1} · классов {m.classes.length} · тест {m.test_rows}</span></div>)}
          </div>
        </div>
        <div className="card">
          <h2>По отраслям ({MODEL_LABELS[best.model]})</h2>
          <table><thead><tr><th>Отрасль</th><th className="num">Прогнозов</th><th className="num">MAE / оборот</th><th className="num">MASE</th><th className="num">Покрытие</th><th className="num">Доля разрывов</th></tr></thead>
            <tbody>{data.summary.by_industry.filter((r: any) => r.model === best.model).map((r: any) => <tr key={r.industry}><td>{r.industry}</td><td className="num">{r.n}</td><td className="num">{fmtPct(r.mae_norm, 1)}</td><td className="num">{r.mase}</td><td className="num">{fmtPct(r.coverage80)}</td><td className="num">{fmtPct(r.gap_rate)}</td></tr>)}</tbody></table>
        </div>
        <div className="small muted" style={{ color: muted }}>Запуск: python manage.py train_models --train-until {data.config.test_start} && python manage.py run_backtest</div>
      </div>
    </>
  )
}
