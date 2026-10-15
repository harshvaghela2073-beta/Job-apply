import { useCallback, useState } from 'react'

const STORAGE_PREFIX = 'job-apply-section:'

function readOpen(id: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(STORAGE_PREFIX + id)
    return value === null ? fallback : value === 'open'
  } catch {
    return fallback
  }
}

function writeOpen(id: string, open: boolean) {
  try {
    localStorage.setItem(STORAGE_PREFIX + id, open ? 'open' : 'closed')
  } catch {
    // Storage unavailable (private mode, blocked): the section still toggles for this visit.
  }
}

export interface SectionOpen {
  open: boolean
  setOpen: (open: boolean) => void
  toggle: () => void
}

/** Open/closed state of a collapsible section, remembered per `id` in localStorage. */
export function useSectionOpen(id: string, defaultOpen = true): SectionOpen {
  const [open, setOpenState] = useState(() => readOpen(id, defaultOpen))
  const setOpen = useCallback(
    (next: boolean) => {
      setOpenState(next)
      writeOpen(id, next)
    },
    [id],
  )
  const toggle = useCallback(() => setOpen(!open), [open, setOpen])
  return { open, setOpen, toggle }
}
