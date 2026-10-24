import type { ForecastEvent } from '../types'
import { fmtDate, fmtMoney, fmtPct, sourceLabel } from '../format'

export default function EventsTable({ events, limit = 15 }: { events: ForecastEvent[]; limit?: number }) {
  const rows = [...events].sort((a, b) => a.date.localeCompare(b.date)).slice(0, limit)
  if (!rows.length) return <div className="empty">Ожидаемых событий нет — добавьте счета или ручные события в календаре</div>
  return (
    <table>
      <thead><tr><th>Дата</th><th>Событие</th><th>Источник</th><th className="num">Сумма</th><th className="num">Уверенность</th></tr></thead>
      <tbody>
        {rows.map((e, i) => (
          <tr key={i}>
            <td>{fmtDate(e.date)}</td>
            <td>{e.label}</td>
            <td><span className="source-tag">{sourceLabel(e.source)}</span></td>
            <td className={`num ${e.direction === 'Поступление' ? 'pos' : 'neg'}`}>{e.direction === 'Поступление' ? '+' : '−'}{fmtMoney(e.amount)}</td>
            <td className="num muted">{fmtPct(e.probability ?? e.confidence)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
