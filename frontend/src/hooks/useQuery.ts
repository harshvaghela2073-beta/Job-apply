import { useCallback, useEffect, useRef, useState } from 'react'

export interface QueryResult<T> {
  /** Data for the current key. Kept while a reload of the same key is in flight. */
  data: T | undefined
  /** Error from the latest request for the current key (cleared while a retry is in flight). */
  error: unknown
  /** True while a request for the current key is in flight. */
  loading: boolean
  reload: () => void
}

interface Settled<T> {
  key: string
  token: number
  data: T | undefined
  error: unknown
}

/**
 * Minimal data-fetching hook.
 * `key` identifies the request (include every parameter the fetcher uses); when it changes the
 * old data is dropped and a fresh request is made. `reload()` refetches the same key and keeps the
 * current data visible until the new response arrives.
 */
export function useQuery<T>(key: string, fetcher: (signal: AbortSignal) => Promise<T>): QueryResult<T> {
  const fetcherRef = useRef(fetcher)
  useEffect(() => {
    fetcherRef.current = fetcher
  })

  const [token, setToken] = useState(0)
  const [settled, setSettled] = useState<Settled<T> | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetcherRef.current(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSettled({ key, token, data, error: undefined })
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        setSettled((prev) => ({ key, token, data: prev?.key === key ? prev.data : undefined, error }))
      },
    )
    return () => controller.abort()
  }, [key, token])

  const reload = useCallback(() => setToken((t) => t + 1), [])

  const sameKey = settled !== null && settled.key === key
  const current = sameKey && settled.token === token
  return {
    data: sameKey ? settled.data : undefined,
    error: current ? settled.error : undefined,
    loading: !current,
    reload,
  }
}
