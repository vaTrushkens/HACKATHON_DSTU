import axios from 'axios'

export const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api'

export const api = axios.create({ baseURL: API_URL })

const ACCESS = 'cf_access'
const REFRESH = 'cf_refresh'

export const tokens = {
  get access() { try { return localStorage.getItem(ACCESS) } catch { return null } },
  get refresh() { try { return localStorage.getItem(REFRESH) } catch { return null } },
  set(a: string, r: string) { try { localStorage.setItem(ACCESS, a); localStorage.setItem(REFRESH, r) } catch { /* private mode */ } },
  clear() { try { localStorage.removeItem(ACCESS); localStorage.removeItem(REFRESH) } catch { /* noop */ } },
}

api.interceptors.request.use((cfg) => {
  const t = tokens.access
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

let refreshing: Promise<string | null> | null = null
api.interceptors.response.use(
  (r) => r,
  async (error) => {
    const original = error.config
    if (error.response?.status === 401 && !original._retry && tokens.refresh) {
      original._retry = true
      refreshing ??= axios
        .post(`${API_URL}/auth/token/refresh/`, { refresh: tokens.refresh })
        .then((r) => { tokens.set(r.data.access, tokens.refresh!); return r.data.access as string })
        .catch(() => { tokens.clear(); return null })
        .finally(() => { refreshing = null })
      const access = await refreshing
      if (access) {
        original.headers.Authorization = `Bearer ${access}`
        return api(original)
      }
      window.location.href = '/login'
    }
    return Promise.reject(error)
  },
)

export async function login(username: string, password: string) {
  const r = await axios.post(`${API_URL}/auth/token/`, { username, password })
  tokens.set(r.data.access, r.data.refresh)
}

export const get = <T = any>(url: string, params?: any) => api.get<T>(url, { params }).then((r) => r.data)
export const post = <T = any>(url: string, body?: any, params?: any) => api.post<T>(url, body, { params }).then((r) => r.data)
export const patch = <T = any>(url: string, body?: any) => api.patch<T>(url, body).then((r) => r.data)
export const del = (url: string) => api.delete(url)
