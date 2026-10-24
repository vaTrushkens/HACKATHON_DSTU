const rub = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 0 })
const num = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 })

export const fmtMoney = (v: number | string | null | undefined) => (v === null || v === undefined || v === '' ? '—' : rub.format(Number(v)))
export const fmtShort = (v: number) => {
  const a = Math.abs(v)
  if (a >= 1e6) return `${(v / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} млн`
  if (a >= 1e3) return `${Math.round(v / 1e3).toLocaleString('ru-RU')} тыс.`
  return num.format(v)
}
export const fmtNum = (v: number) => num.format(v)
export const fmtPct = (v: number | null | undefined, digits = 0) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`)

const months = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
export const fmtDate = (s: string | null | undefined) => {
  if (!s) return '—'
  const d = new Date(s)
  return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`
}
export const fmtDateShort = (s: string) => { const d = new Date(s); return `${d.getDate()} ${months[d.getMonth()]}` }
export const daysBetween = (a: string, b: string) => Math.round((new Date(b).getTime() - new Date(a).getTime()) / 86400000)
export const addDays = (s: string, n: number) => { const d = new Date(s); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10) }

export const SOURCE_LABELS: Record<string, string> = {
  recurring: 'из выписки', tax_calendar: 'налоговый календарь', manual: 'ручной ввод', 'invoice:1c': 'счёт (1С/ЭДО)', '1c': 'счёт из 1С', edo: 'УПД из ЭДО', ofd: 'отчёт ОФД',
  bank: 'данные банка', scenario: 'сценарий', registry: 'реестр', contract: 'договор', industry_profile: 'отраслевой профиль',
}
export const MODE_LABELS: Record<string, [string, string]> = { api: ['API подключён', 'good'], derived: ['восстановлено из выписки', 'info'], mock: ['не подключено — нет ключа', 'medium'], file: ['импорт файла', 'info'], active: ['активно', 'good'] }
export const sourceLabel = (s: string) => SOURCE_LABELS[s] ?? s
export const PERIOD_LABELS: Record<string, string> = { weekly: 'еженедельно', biweekly: 'раз в 2 недели', monthly: 'ежемесячно', quarterly: 'ежеквартально' }
export const escapeHtml = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!)
