import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { CountsContext, type CountsValue } from '../hooks/useCounts'
import { useQuery } from '../hooks/useQuery'

/** Loads the dashboard summary once for the app; refreshes it on every navigation. */
export function CountsProvider({ children }: { children: ReactNode }) {
  const summary = useQuery('summary', (signal) => api.summary(signal))
  const refresh = summary.reload

  // Refresh when the user moves between pages (skip the first render: data is fresh).
  const { pathname } = useLocation()
  const firstRender = useRef(true)
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    refresh()
  }, [pathname, refresh])

  const value = useMemo<CountsValue>(() => ({ summary, refresh }), [summary, refresh])
  return <CountsContext.Provider value={value}>{children}</CountsContext.Provider>
}
