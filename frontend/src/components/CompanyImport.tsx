import { useState } from 'react'
import { post } from '../api'
import { useAuth } from '../auth'

export default function CompanyImport() {
  const { refreshCompanies, selectCompany } = useAuth()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const submit = async () => {
    setBusy(true); setError(''); setMessage('')
    try {
      let id: number
      if (file) {
        const body = new FormData(); body.append('file', file)
        const result = await post('/companies/import-dataset/', body)
        id = result.companies[0].company.id
        const total = result.companies.reduce((sum: number, item: any) => sum + item.report.rows_new, 0)
        const bad = result.companies.reduce((sum: number, item: any) => sum + item.report.rows_bad, 0)
        setMessage(`Создан отдельный набор: компаний ${result.companies.length}, операций ${total}, пропущено некорректных строк ${bad}.`)
      } else {
        const company = await post('/companies/', { name })
        id = company.id; setMessage('Компания создана. Загрузите её выписку в разделе «Операции».')
      }
      await refreshCompanies(); selectCompany(id); setFile(null)
    } catch (e: any) { setError(typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : JSON.stringify(e?.response?.data ?? 'Не удалось добавить данные')) }
    finally { setBusy(false) }
  }
  return <>
    <button className="secondary" onClick={() => setOpen(true)}>Добавить данные</button>
    {open && <div role="dialog" aria-modal="true" aria-label="Добавить финансовые данные" style={{ position: 'fixed', inset: 0, background: '#0006', display: 'grid', placeItems: 'center', zIndex: 1000 }}>
      <div className="card" style={{ width: 'min(560px, 94vw)', maxHeight: '90vh', overflow: 'auto' }}>
        <h2>Добавить финансовые данные</h2>
        <p>Загрузите CSV одной или нескольких компаний либо ZIP с companies.csv и statements/*.csv. Для каждой компании будет создана отдельная история.</p>
        <label className="field">Новый набор выписок<input type="file" accept=".csv,.txt,.zip" onChange={e => setFile(e.target.files?.[0] ?? null)} disabled={busy} /></label>
        {!file && <label className="field">Или создайте компанию без выписки<input value={name} onChange={e => setName(e.target.value)} placeholder="Название компании" /></label>}
        {error && <p className="error">{error}</p>}
        {message && <p role="status">{message}</p>}
        <div className="toolbar"><button onClick={submit} disabled={busy || (!file && !name.trim())}>{busy ? 'Обрабатываем данные…' : file ? 'Загрузить новый набор' : 'Создать компанию'}</button><button className="secondary" onClick={() => setOpen(false)} disabled={busy}>Закрыть</button></div>
      </div>
    </div>}
  </>
}
