import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth'

const links = [
  ['/', 'Обзор'],
  ['/operations', 'Операции'],
  ['/calendar', 'Календарь платежей'],
  ['/counterparties', 'Контрагенты'],
  ['/scenarios', 'Что если'],
  ['/plan-fact', 'План-факт'],
  ['/quality', 'Качество прогноза'],
]

export default function Layout({ children }: { children: React.ReactNode }) {
  const { companies, company, selectCompany, user, logout } = useAuth()
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand"><span />Денежный поток</div>
        <select className="company-select" value={company?.id ?? ''} onChange={(e) => selectCompany(Number(e.target.value))}>
          {companies.map((c) => <option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}
        </select>
        {company && <div className="small muted" style={{ padding: '0 4px 8px' }}>{company.industry}</div>}
        <nav className="nav">
          {links.map(([to, label]) => <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>)}
        </nav>
        <div className="bottom">
          <div>{user?.username}</div>
          <button className="ghost" onClick={logout} style={{ padding: 0 }}>Выйти</button>
        </div>
      </aside>
      <main className="main">{company ? children : <div className="empty">Нет доступных компаний. Загрузите выписку или обратитесь к администратору.</div>}</main>
    </div>
  )
}
