import { useEffect, useState } from 'react'
import type { JobRemoval } from '../hooks/useJobRemoval'

/** How long Undo is offered after a removal. */
const UNDO_MS = 6000

/** "Removed · Undo" at the bottom of the screen for a few seconds; held open while pointed at or focused. */
export function RemovedToast({ removal }: { removal: Pick<JobRemoval, 'toast' | 'undo' | 'dismiss'> }) {
  const { toast, undo, dismiss } = removal
  // Key of the toast the pointer or keyboard focus is on (its timer waits).
  const [held, setHeld] = useState<number | null>(null)
  const paused = toast !== null && held === toast.key

  useEffect(() => {
    if (!toast || paused) return
    const timer = window.setTimeout(dismiss, UNDO_MS)
    return () => window.clearTimeout(timer)
  }, [toast, paused, dismiss])

  return (
    <div className="toast-region" aria-live="polite">
      {toast && (
        <div
          key={toast.key}
          className="toast"
          onPointerEnter={() => setHeld(toast.key)}
          onPointerLeave={() => setHeld(null)}
          onFocus={() => setHeld(toast.key)}
          onBlur={() => setHeld(null)}
        >
          <span>
            {toast.error ? 'Not restored' : 'Removed'}
            {toast.error && <span className="sr-only">: {toast.error}</span>}
          </span>
          <span className="toast-sep" aria-hidden="true">
            ·
          </span>
          <button
            type="button"
            className="toast-action"
            onClick={(e) => {
              // Focus goes back to the restored row's ×, not to the page.
              const keepFocus = document.activeElement === e.currentTarget
              const { id } = toast
              undo()
              if (keepFocus)
                requestAnimationFrame(() =>
                  document.querySelector<HTMLButtonElement>(`.row-remove[data-job-id="${id}"]`)?.focus(),
                )
            }}
          >
            Undo
          </button>
        </div>
      )}
    </div>
  )
}
