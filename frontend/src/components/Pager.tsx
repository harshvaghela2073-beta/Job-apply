interface PagerProps {
  total: number
  offset: number
  limit: number
  shown: number
  onChange: (offset: number) => void
}

/** "1–50 of 120" with previous/next buttons. Hidden when everything fits on one page. */
export function Pager({ total, offset, limit, shown, onChange }: PagerProps) {
  if (total <= limit && offset === 0) {
    return <div className="pager muted">{total === 1 ? '1 item' : `${total} items`}</div>
  }
  const from = total === 0 ? 0 : offset + 1
  const to = offset + shown
  return (
    <div className="pager">
      <span className="muted">
        {from}–{to} of {total}
      </span>
      <button type="button" className="btn btn-sm" disabled={offset === 0} onClick={() => onChange(Math.max(offset - limit, 0))}>
        ← Prev
      </button>
      <button type="button" className="btn btn-sm" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Next →
      </button>
    </div>
  )
}
