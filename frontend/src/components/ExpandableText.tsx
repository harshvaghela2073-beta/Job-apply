import { useId, useLayoutEffect, useRef, useState } from 'react'

/**
 * Multi-line text (line breaks kept) that shows only its first `lines` lines until expanded.
 * The toggle appears only when the text is actually cut off at the current width.
 */
export function ExpandableText({ text, lines = 4 }: { text: string; lines?: number }) {
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const id = useId()

  useLayoutEffect(() => {
    const el = ref.current
    if (!el || expanded) return
    const check = () => setOverflows(el.scrollHeight > el.clientHeight + 1)
    check()
    const observer = new ResizeObserver(check)
    observer.observe(el)
    return () => observer.disconnect()
  }, [text, lines, expanded])

  return (
    <div className="message-text">
      <div
        ref={ref}
        id={id}
        className={expanded ? undefined : 'clamp-lines'}
        style={expanded ? undefined : { WebkitLineClamp: lines }}
      >
        {text}
      </div>
      {(overflows || expanded) && (
        <button
          type="button"
          className="expand-btn"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show less' : 'Show all'}
        </button>
      )}
    </div>
  )
}
