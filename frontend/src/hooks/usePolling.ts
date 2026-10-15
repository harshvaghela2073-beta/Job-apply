import { useEffect, useRef, useState } from 'react'
import type { QueryResult } from './useQuery'

/**
 * Reloads a query every `intervalMs` (null pauses it), and right away when the tab becomes
 * visible again. A tick is skipped while a request is still in flight, so a slow backend is never
 * cut off by the next poll. Changing the interval restarts the timer.
 */
export function usePolling(query: Pick<QueryResult<unknown>, 'loading' | 'reload'>, intervalMs: number | null) {
  const queryRef = useRef(query)
  useEffect(() => {
    queryRef.current = query
  })

  useEffect(() => {
    if (intervalMs === null) return
    const tick = () => {
      if (!queryRef.current.loading) queryRef.current.reload()
    }
    const timer = window.setInterval(tick, intervalMs)
    const onVisible = () => {
      if (document.visibilityState === 'visible') tick()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [intervalMs])
}

/** Current time in ms, updated every `intervalMs`; for "3 min ago" labels that should stay fresh. */
export function useNow(intervalMs = 60_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}
