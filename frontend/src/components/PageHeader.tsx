import type { ReactNode } from 'react'

interface PageHeaderProps {
  title: string
  description?: ReactNode
  actions?: ReactNode
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p className="muted">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

export function RefreshButton({ onClick, loading }: { onClick: () => void; loading: boolean }) {
  return (
    <button type="button" className="btn btn-sm" onClick={onClick} disabled={loading} title="Reload from the backend">
      {loading ? 'Refreshing…' : '↻ Refresh'}
    </button>
  )
}
