import { useCallback, useState } from 'react'
import { errorMessage } from '../api/client'

export type ActionResult<R> = { ok: true; value: R } | { ok: false; error: unknown }

export interface Action<A extends unknown[], R> {
  run: (...args: A) => Promise<ActionResult<R>>
  pending: boolean
  error: string | null
  clearError: () => void
}

/**
 * Wraps a mutation: tracks `pending` (to disable its button) and keeps the last error message.
 * `run` never throws; it resolves to `{ ok: false, error }` on failure.
 */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>): Action<A, R> {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(
    async (...args: A): Promise<ActionResult<R>> => {
      setPending(true)
      setError(null)
      try {
        const value = await fn(...args)
        return { ok: true, value }
      } catch (err) {
        setError(errorMessage(err))
        return { ok: false, error: err }
      } finally {
        setPending(false)
      }
    },
    [fn],
  )

  const clearError = useCallback(() => setError(null), [])
  return { run, pending, error, clearError }
}
