import { useId, useState, type MouseEvent, type ReactNode } from 'react'

/** localStorage key prefix; each section remembers whether it is open under `<prefix><id>`. */
const STORAGE_PREFIX = 'job-apply-section:'

function readOpen(id: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(STORAGE_PREFIX + id)
    return value === '1' ? true : value === '0' ? false : fallback
  } catch {
    return fallback
  }
}

function storeOpen(id: string, open: boolean) {
  try {
    localStorage.setItem(STORAGE_PREFIX + id, open ? '1' : '0')
  } catch {
    // Storage unavailable (private mode, blocked): the state lasts until the page reloads.
  }
}

/** Clicks on these inside the header do their own thing and don't toggle the section. */
const INTERACTIVE = 'button, a, input, select, textarea, label, [role="button"]'

interface CollapsibleSectionProps {
  /** Stable, unique key: the open/closed state is remembered per id. */
  id: string
  title: ReactNode
  /** Short data next to the title (a count, the scan schedule); visible while closed too. */
  meta?: ReactNode
  /** Controls at the right of the header, before the arrow. Clicking them doesn't toggle. */
  actions?: ReactNode
  defaultOpen?: boolean
  className?: string
  /** Rendered only while open. */
  children: ReactNode
}

/** Section whose header (or the arrow at its right) opens and closes the body. */
export function CollapsibleSection({
  id,
  title,
  meta,
  actions,
  defaultOpen = true,
  className,
  children,
}: CollapsibleSectionProps) {
  const [open, setOpen] = useState(() => readOpen(id, defaultOpen))
  const headingId = useId()
  const bodyId = useId()

  const toggle = () => {
    const next = !open
    setOpen(next)
    storeOpen(id, next)
  }

  // Mouse convenience: a click anywhere on the header toggles. Keyboard users use the arrow button.
  const onHeaderClick = (e: MouseEvent<HTMLDivElement>) => {
    if (e.target instanceof Element && e.target.closest(INTERACTIVE)) return
    toggle()
  }

  return (
    <section
      className={`collapsible${open ? ' is-open' : ''}${className ? ` ${className}` : ''}`}
      aria-labelledby={headingId}
    >
      <div className="collapsible-head" onClick={onHeaderClick}>
        <h2 className="section-title" id={headingId}>
          {title}
        </h2>
        {meta !== undefined && meta !== null && <div className="collapsible-meta">{meta}</div>}
        <div className="collapsible-actions">
          {actions}
          <button
            type="button"
            className="collapse-btn"
            aria-expanded={open}
            aria-controls={bodyId}
            aria-labelledby={headingId}
            title={open ? 'Collapse' : 'Expand'}
            onClick={toggle}
          >
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">
              <path
                d="M4 6l4 4 4-4"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>
      <div id={bodyId} className="collapsible-body" hidden={!open}>
        {open && children}
      </div>
    </section>
  )
}
