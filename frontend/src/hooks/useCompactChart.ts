import { useEffect, useState } from 'react'

export default function useCompactChart() {
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 600px)').matches)
  useEffect(() => {
    const query = window.matchMedia('(max-width: 600px)')
    const update = () => setCompact(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return compact
}
