import { useEffect, useState } from 'react'
import { get } from '../api'
import { useAuth } from '../auth'
import { fmtDate, fmtMoney, fmtPct } from '../format'

export default function Counterparties() {
  const { company } = useAuth()
  const [data, setData] = useState<any>(null)
  useEffect(() => { if (company) get(`/companies/${company.id}/counterparties/`).then(setData) }, [company?.id])
  if (!company) return null
  const table = (rows: any[], clients: boolean) => (
    <table>
      <thead><tr><th>Контрагент</th><th className="num">Оборот за 6 мес.</th><th className="num">Доля</th><th className="num">Операций</th><th>Последняя</th>{clients && <><th className="num">Задержка оплаты</th><th>Реестр</th></>}</tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.inn + r.name}>
          <td>{r.name}<div className="small muted">{r.inn || 'без ИНН'}</div></td>
          <td className="num">{fmtMoney(r.total_6m)}</td>
          <td className="num"><div className="progress" style={{ width: 70, display: 'inline-block', marginRight: 6 }}><i style={{ width: `${Math.min(100, r.share * 100)}%` }} /></div>{fmtPct(r.share)}</td>
          <td className="num">{r.n}</td>
          <td>{fmtDate(r.last_date)}</td>
          {clients && <>
            <td className="num">{r.median_delay !== null ? <>{Math.round(r.median_delay)} дн. <span className="small muted">(до {Math.round(r.p90_delay)})</span></> : <span className="muted">—</span>}</td>
            <td>{r.registry && <span className={`badge ${r.registry.risk_level === 'high' ? 'high' : r.registry.risk_level === 'medium' ? 'medium' : 'good'}`}>{r.registry.status}</span>}</td>
          </>}
        </tr>
      ))}</tbody>
    </table>
  )
  return (
    <>
      <div className="page-head"><div><h1>Контрагенты</h1><p className="subtitle">Концентрация выручки, типичная задержка оплаты по счетам (из назначения платежа) и статус в открытых реестрах. Задержки используются при планировании выставленных счетов, статус — снижает вероятность оплаты.</p></div></div>
      {data && <div className="grid"><div className="card"><h2>Клиенты</h2>{table(data.clients, true)}</div><div className="card"><h2>Поставщики</h2>{table(data.suppliers, false)}</div></div>}
    </>
  )
}
