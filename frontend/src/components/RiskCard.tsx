import type { Risk } from '../types'
import { fmtDate, fmtMoney, fmtPct } from '../format'

const LEVEL = { high: 'Высокий риск', medium: 'Есть риск', low: 'Риск низкий' }

export default function RiskCard({ risk, asOf }: { risk: Risk; asOf: string }) {
  const days = risk.gap_date ? Math.round((new Date(risk.gap_date).getTime() - new Date(asOf).getTime()) / 86400000) : null
  return (
    <div className={`card risk-card ${risk.level}`}>
      <div className="risk-head">
        <span className={`badge ${risk.level}`}>{risk.level === 'high' ? '●' : risk.level === 'medium' ? '▲' : '✓'} {LEVEL[risk.level]}</span>
        <span className="muted small">вероятность нехватки денег: {fmtPct(risk.probability)}</span>
      </div>
      <div className="risk-summary">{risk.summary}</div>
      <p className="explain">{risk.explanation}</p>
      <div className="kpi-row" style={{ marginTop: 10 }}>
        <div className="stat"><span className="label">Дата возможного разрыва</span><span className="value" style={{ fontSize: 18 }}>{risk.gap_date ? fmtDate(risk.gap_date) : risk.gap_date_p10 ? `${fmtDate(risk.gap_date_p10)} (в плохом случае)` : '—'}</span>{days !== null && <span className="hint">через {days} дн.</span>}</div>
        <div className="stat"><span className="label">Не хватит (ожидаемо)</span><span className="value" style={{ fontSize: 18 }}>{risk.deficit_p50 > 0 ? fmtMoney(risk.deficit_p50) : '—'}</span><span className="hint">в плохом случае до {fmtMoney(risk.deficit_p10)}</span></div>
        <div className="stat"><span className="label">Денег хватит на</span><span className="value" style={{ fontSize: 18 }}>{risk.runway_days !== null ? `${risk.runway_days} дн.` : 'весь горизонт'}</span><span className="hint">по медианному прогнозу</span></div>
      </div>
    </div>
  )
}
