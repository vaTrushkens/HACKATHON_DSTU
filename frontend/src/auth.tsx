import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { get, login as apiLogin, tokens } from './api'
import type { Company, User } from './types'

type Ctx = {
  user: User | null
  loading: boolean
  companies: Company[]
  company: Company | null
  selectCompany: (id: number) => void
  refreshCompanies: () => Promise<void>
  login: (u: string, p: string) => Promise<void>
  logout: () => void
}

const AuthCtx = createContext<Ctx>(null as any)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [companies, setCompanies] = useState<Company[]>([])
  const [companyId, setCompanyId] = useState<number | null>(() => {
    try { const v = localStorage.getItem('cf_company'); return v ? Number(v) : null } catch { return null }
  })
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    if (!tokens.access) { setLoading(false); return }
    try {
      const me = await get<User>('/auth/me/')
      setUser(me)
      const list = await get<any>('/companies/', { page_size: 200 })
      const items: Company[] = list.results ?? list
      setCompanies(items)
      setCompanyId((cur) => (cur && items.some((c) => c.id === cur) ? cur : items[0]?.id ?? null))
    } catch { tokens.clear() } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const value = useMemo<Ctx>(() => ({
    user, loading, companies,
    company: companies.find((c) => c.id === companyId) ?? null,
    selectCompany: (id) => { setCompanyId(id); try { localStorage.setItem('cf_company', String(id)) } catch { /* noop */ } },
    refreshCompanies: async () => { const list = await get<any>('/companies/', { page_size: 200 }); setCompanies(list.results ?? list) },
    login: async (u, p) => { await apiLogin(u, p); setLoading(true); await load() },
    logout: () => { tokens.clear(); setUser(null); setCompanies([]) },
  }), [user, loading, companies, companyId, load])

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>
}

export const useAuth = () => useContext(AuthCtx)
