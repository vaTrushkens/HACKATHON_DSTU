import { useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import { del, get, post } from '../api'
import { useAuth } from '../auth'

type Msg = { id?: number; role: 'user' | 'assistant'; content: string; tools_used?: { name: string }[]; source?: string }
type TgStatus = { linked: boolean; username?: string; company?: string | null; bot_username?: string }
type LinkCode = { code: string; deep_link: string | null; bot_username: string }

const QUICK = ['Когда возможен кассовый разрыв?', 'Какие платежи на этой неделе?', 'Что если клиент задержит оплату на 14 дней?', 'Как работает страница «Что если»?']
const TOOL_LABELS: Record<string, string> = { get_forecast_summary: 'прогноз', get_upcoming_payments: 'платежи', run_what_if: 'что если', add_planned_event: 'событие добавлено', get_category_breakdown: 'структура расходов', get_tax_calendar: 'налоги' }

export default function ChatWidget() {
  const { company } = useAuth()
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState<{ name: string; enabled: boolean } | null>(null)
  const [tg, setTg] = useState<TgStatus | null>(null)
  const [link, setLink] = useState<LinkCode | null>(null)
  const [qr, setQr] = useState('')
  const [justLinked, setJustLinked] = useState(false)
  const [err, setErr] = useState('')
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open || !company) return
    setLink(null); setErr('')
    get(`/companies/${company.id}/assistant/chat/`).then((r) => { setMsgs(r.messages); setModel({ name: r.model, enabled: r.enabled }) }).catch(() => setErr('Не удалось загрузить историю'))
    get<TgStatus>('/telegram/link/').then(setTg).catch(() => {})
  }, [open, company?.id])
  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }) }, [msgs, busy, open])

  useEffect(() => {
    if (!link?.deep_link) { setQr(''); return }
    QRCode.toDataURL(link.deep_link, { width: 512, margin: 1, errorCorrectionLevel: 'M' }).then(setQr).catch(() => setQr(''))
  }, [link?.deep_link])

  // Пока открыт QR, опрашиваем статус: как только бот получил /start с кодом — закрываем экран сами.
  useEffect(() => {
    if (!link) return
    const timer = setInterval(() => {
      get<TgStatus>('/telegram/link/').then((s) => { if (s.linked) { setTg(s); setLink(null); setJustLinked(true) } }).catch(() => {})
    }, 3000)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setLink(null) }
    window.addEventListener('keydown', onKey)
    return () => { clearInterval(timer); window.removeEventListener('keydown', onKey) }
  }, [link])
  useEffect(() => { if (!justLinked) return; const t = setTimeout(() => setJustLinked(false), 4000); return () => clearTimeout(t) }, [justLinked])

  if (!company) return null

  const send = async (q: string) => {
    const message = q.trim()
    if (!message || busy) return
    setText(''); setErr('')
    setMsgs((m) => [...m, { role: 'user', content: message }])
    setBusy(true)
    try {
      const r = await post(`/companies/${company.id}/assistant/chat/`, { message })
      setMsgs((m) => [...m, { role: 'assistant', content: r.reply, tools_used: r.tools_used, source: r.source }])
    } catch (e: any) { setErr(e?.response?.data?.detail || 'Ассистент не ответил') } finally { setBusy(false) }
  }
  const clear = async () => { await del(`/companies/${company.id}/assistant/chat/`); setMsgs([]) }
  const connectTg = async () => {
    try { setLink(await post<LinkCode>(`/companies/${company.id}/telegram/link-code/`)) } catch { setErr('Не удалось получить код для Telegram') }
  }
  const disconnectTg = async () => {
    if (!window.confirm('Отключить Telegram? Уведомления перестанут приходить.')) return
    await del('/telegram/link/'); setTg({ linked: false, bot_username: tg?.bot_username })
  }

  return (
    <>
      <button className="chat-fab" aria-label={open ? 'Закрыть ассистента' : 'Открыть ассистента'} aria-expanded={open} onClick={() => setOpen(!open)}>
        {open
          ? <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
          : <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H8l-4 4V5z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" /><path d="M8 9h8M8 12h5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>}
      </button>
      {open && (
        <section className="chat-panel" aria-label="Ассистент">
          <div className="chat-head">
            <h3>Ассистент</h3>
            {model && <span className={`badge ${model.enabled ? 'good' : 'medium'}`} title={model.enabled ? 'Локальная модель отвечает' : 'Модель недоступна — ответы по правилам'}>{model.enabled ? model.name : 'offline'}</span>}
            {tg?.linked && (
              <button className="chat-tg-chip" onClick={disconnectTg} title={`Telegram подключён${tg.username ? `: @${tg.username}` : ''} · уведомления включены. Нажмите, чтобы отключить`}>
                <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 4L3 11l6 2 2 6 3-4 5 3z" fill="currentColor" /></svg>
                {tg.username ? `@${tg.username}` : 'Telegram'}
              </button>
            )}
            {msgs.length > 0 && <button className="ghost" onClick={clear} title="Очистить историю">Очистить</button>}
          </div>
          {justLinked && <div className="chat-tg chat-tg-ok">Telegram подключён — уведомления о рисках и платежах будут приходить в бота.</div>}
          {tg && !tg.linked && (
            <div className="chat-tg">
              <span>Уведомления о рисках и напоминания о платежах в Telegram</span>
              <button className="secondary" onClick={connectTg}>Подключить Telegram</button>
            </div>
          )}
          <div className="chat-messages" role="log" aria-live="polite">
            {msgs.length === 0 && !busy && <div className="chat-msg assistant">Спросите о прогнозе, платежах, налогах или о том, как пользоваться сервисом. Все цифры беру из тех же расчётов, что и на экране.</div>}
            {msgs.map((m, i) => (
              <div key={m.id ?? `n${i}`} className={`chat-msg ${m.role}`}>
                {m.content}
                {m.role === 'assistant' && ((m.tools_used?.length ?? 0) > 0 || m.source === 'rules') && (
                  <div className="chat-tools">
                    {m.tools_used?.map((t, j) => <span key={j}>{TOOL_LABELS[t.name] ?? t.name}</span>)}
                    {m.source === 'rules' && <span>без модели</span>}
                  </div>
                )}
              </div>
            ))}
            {busy && <div className="chat-typing">Считаю прогноз и формулирую ответ…</div>}
            {err && <div className="error small">{err}</div>}
            <div ref={bottom} />
          </div>
          {msgs.length === 0 && <div className="chat-quick">{QUICK.map((q) => <button key={q} className="secondary" onClick={() => send(q)}>{q}</button>)}</div>}
          <form className="chat-form" onSubmit={(e) => { e.preventDefault(); send(text) }}>
            <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Ваш вопрос…" disabled={busy} aria-label="Сообщение ассистенту" />
            <button type="submit" disabled={busy || !text.trim()}>Отправить</button>
          </form>
        </section>
      )}
      {link && (
        <div className="chat-qr-modal" role="dialog" aria-modal="true" aria-label="Подключение Telegram" onClick={(e) => { if (e.target === e.currentTarget) setLink(null) }}>
          <div className="chat-qr-card">
            <h2>Подключение Telegram</h2>
            {qr ? <img className="chat-qr-big" src={qr} alt={`QR-код: откройте бота ${link.bot_username} с кодом ${link.code}`} /> : <div className="chat-qr-big chat-qr-empty">{link.deep_link ? 'Готовим QR…' : 'Бот не настроен (TELEGRAM_BOT_TOKEN)'}</div>}
            <p>Отсканируйте QR камерой телефона и нажмите «Start» в боте. Код <b>{link.code}</b> действует 15 минут — экран закроется сам, как только аккаунт привяжется.</p>
            <div className="row" style={{ justifyContent: 'center' }}>
              {link.deep_link && <a className="btn" href={link.deep_link} target="_blank" rel="noreferrer">Открыть @{link.bot_username} на этом устройстве</a>}
              <button className="secondary" onClick={() => setLink(null)}>Закрыть</button>
            </div>
            <div className="chat-qr-wait muted small">Ждём подтверждение от бота…</div>
          </div>
        </div>
      )}
    </>
  )
}
