import { useEffect, useState } from 'react'

export type Theme = 'auto' | 'light' | 'dark'
const KEY = 'theme'
const media = window.matchMedia('(prefers-color-scheme: dark)')

export function readTheme(): Theme {
  const saved = localStorage.getItem(KEY)
  return saved === 'light' || saved === 'dark' ? saved : 'auto'
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement
  if (theme === 'auto') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}

export function resolveTheme(theme: Theme): 'light' | 'dark' {
  return theme === 'auto' ? (media.matches ? 'dark' : 'light') : theme
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readTheme)
  const [systemDark, setSystemDark] = useState(media.matches)
  useEffect(() => {
    const update = () => setSystemDark(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  const setTheme = (next: Theme) => {
    localStorage.setItem(KEY, next)
    applyTheme(next)
    setThemeState(next)
  }
  const resolved: 'light' | 'dark' = theme === 'auto' ? (systemDark ? 'dark' : 'light') : theme
  return { theme, setTheme, resolved }
}
