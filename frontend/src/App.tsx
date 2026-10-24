import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth'
import Layout from './components/Layout'
import Calendar from './pages/Calendar'
import Counterparties from './pages/Counterparties'
import Dashboard from './pages/Dashboard'
import Login from './pages/Login'
import Operations from './pages/Operations'
import PlanFact from './pages/PlanFact'
import Quality from './pages/Quality'
import Scenarios from './pages/Scenarios'

export default function App() {
  const { user, loading } = useAuth()
  if (loading) return <div className="empty">Загрузка…</div>
  if (!user) return <Routes><Route path="*" element={<Login />} /></Routes>
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/operations" element={<Operations />} />
        <Route path="/calendar" element={<Calendar />} />
        <Route path="/counterparties" element={<Counterparties />} />
        <Route path="/scenarios" element={<Scenarios />} />
        <Route path="/plan-fact" element={<PlanFact />} />
        <Route path="/quality" element={<Quality />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </Layout>
  )
}
