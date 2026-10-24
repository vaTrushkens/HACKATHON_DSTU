import type { Alert } from '../types'
import { fmtDate } from '../format'

export default function AlertList({ alerts }: { alerts: Alert[] }) {
  if (!alerts.length) return <div className="empty">Предупреждений нет</div>
  return (
    <div>
      {alerts.map((a, i) => (
        <div key={i} className={`alert ${a.level}`}>
          <span className="dot" />
          <div>
            <div className="title">{a.title} {a.date && <span className="muted small">· {fmtDate(a.date)}</span>}</div>
            <div className="msg">{a.message}</div>
          </div>
        </div>
      ))}
    </div>
  )
}
