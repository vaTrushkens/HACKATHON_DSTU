import Select from './Select'
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
      <header className="site-header">
        <div className="header-inner">
          <NavLink to="/" className="brand" aria-label="Денежный поток — обзор"><span aria-hidden="true" />Денежный поток</NavLink>
          <span className="header-section">Для бизнеса</span>
          <div className="header-account">
            <Select aria-label="Компания" className="company-select" value={company?.id ?? ''} onValueChange={(value) => selectCompany(Number(value))}>
              {companies.map((c) => <option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}
            </Select>
            <span>{user?.username}</span>
            <button className="secondary" onClick={logout}>Выйти</button>
          </div>
        </div>
        <nav className="nav" aria-label="Основная навигация">
          {links.map(([to, label]) => <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>)}
        </nav>
      </header>
      <main className="main">{company ? children : <div className="empty">Нет доступных компаний. Загрузите выписку или обратитесь к администратору.</div>}</main>
    </div>
  )
}
