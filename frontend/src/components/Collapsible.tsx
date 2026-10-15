import { useId, type MouseEvent, type ReactNode } from 'react'
import { useSectionOpen, type SectionOpen } from '../hooks/useSectionOpen'
import { ChevronDownIcon } from './Icons'
import '../templates-profile.css'

interface CollapsibleProps {
  /** Stable id; the open/closed state is remembered under it. */
  id: string
  title: ReactNode
  /** Shown after the title, e.g. a count. */
  meta?: ReactNode
  /** Controls in the header; clicking them doesn't toggle the section. */
  actions?: ReactNode
  /** Open/closed state owned by the parent (from `useSectionOpen`), to open it from outside. */
  state?: SectionOpen
  defaultOpen?: boolean
  className?: string
  children: ReactNode
}

// Clicks on these inside the header do their own thing instead of toggling.
const INTERACTIVE = 'button, a, input, select, textarea, label, summary, [role="button"]'

/**
 * A panel whose body folds away. The arrow button at the right is the accessible toggle;
 * clicking anywhere else on the header toggles too.
 */
export function Collapsible({
  id,
  title,
  meta,
  actions,
  state,
  defaultOpen = true,
  className = '',
  children,
}: CollapsibleProps) {
  const own = useSectionOpen(id, defaultOpen)
  const { open, toggle } = state ?? own
  const uid = useId()
  const titleId = `${uid}-title`
  const bodyId = `${uid}-body`

  const onHeadClick = (e: MouseEvent<HTMLDivElement>) => {
    if (e.target instanceof Element && e.target.closest(INTERACTIVE)) return
    // Leave a text selection in the header alone.
    if (window.getSelection()?.toString()) return
    toggle()
  }

  return (
    <section className={`cl panel${open ? ' cl-open' : ''}${className ? ` ${className}` : ''}`} aria-labelledby={titleId}>
      <div className="cl-head" onClick={onHeadClick}>
        <h2 id={titleId} className="cl-title">
          {title}
        </h2>
        {meta}
        {actions && <div className="cl-actions">{actions}</div>}
        <button
          type="button"
          className="cl-toggle"
          aria-expanded={open}
          aria-controls={bodyId}
          aria-labelledby={titleId}
          title={open ? 'Collapse' : 'Expand'}
          onClick={toggle}
        >
          <ChevronDownIcon />
        </button>
      </div>
      <div id={bodyId} className="cl-body" hidden={!open}>
        {children}
      </div>
    </section>
  )
}
