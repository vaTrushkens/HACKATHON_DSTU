import { useState } from 'react'
import { useAuth } from '../auth'

export default function Login() {
  const { login } = useAuth()
  const [u, setU] = useState('demo')
  const [p, setP] = useState('demo12345')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try { await login(u, p) } catch { setErr('Неверный логин или пароль') } finally { setBusy(false) }
  }
  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <div className="brand"><span />Денежный поток</div>
        <p className="subtitle" style={{ marginBottom: 14 }}>Прогноз остатка и предупреждение о кассовых разрывах для малого бизнеса</p>
        <label className="field">Логин<input value={u} onChange={(e) => setU(e.target.value)} autoFocus /></label>
        <label className="field" style={{ marginTop: 10 }}>Пароль<input type="password" value={p} onChange={(e) => setP(e.target.value)} /></label>
        {err && <div className="error" style={{ marginTop: 10 }}>{err}</div>}
        <button style={{ marginTop: 16, width: '100%' }} disabled={busy}>{busy ? 'Входим…' : 'Войти'}</button>
      </form>
    </div>
  )
}
