import Select from './Select'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth'
import { useTheme, type Theme } from '../theme'
import ChatWidget from './ChatWidget'

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
  const { theme, setTheme, resolved } = useTheme()

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
            <Select aria-label="Тема оформления" className="theme-select" value={theme} onValueChange={(value) => setTheme(value as Theme)}>
              <option value="auto">Тема: авто</option>
              <option value="light">Тема: светлая</option>
              <option value="dark">Тема: тёмная</option>
            </Select>
            <button className="secondary" onClick={logout}>Выйти</button>
          </div>
        </div>
        <nav className="nav" aria-label="Основная навигация">
          {links.map(([to, label]) => <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>)}
        </nav>
      </header>
      {/* Charts read CSS variables at render time, so pages remount when the resolved theme changes. */}
      <main className="main" key={resolved}>{company ? children : <div className="empty">Нет доступных компаний. Загрузите выписку или обратитесь к администратору.</div>}</main>
      <ChatWidget />
    </div>
  )
}
