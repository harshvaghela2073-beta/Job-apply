import type { ReactNode } from 'react'
import { errorMessage } from '../api/client'
import type { QueryResult } from '../hooks/useQuery'

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

export function ErrorState({ error, onRetry, compact = false }: { error: unknown; onRetry?: () => void; compact?: boolean }) {
  return (
    <div className={`state state-error${compact ? ' state-compact' : ''}`} role="alert">
      <span>
        <strong>Couldn't load data.</strong> {errorMessage(error)}
      </span>
      {onRetry && (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <strong>{title}</strong>
      {children && <span className="muted">{children}</span>}
    </div>
  )
}

export function InlineError({ message, onDismiss }: { message: string | null; onDismiss?: () => void }) {
  if (!message) return null
  return (
    <div className="inline-error" role="alert">
      <span>{message}</span>
      {onDismiss && (
        <button type="button" className="link-btn" onClick={onDismiss} aria-label="Dismiss error">
          ×
        </button>
      )}
    </div>
  )
}

interface QueryBoundaryProps<T> {
  query: QueryResult<T>
  isEmpty?: (data: T) => boolean
  empty?: ReactNode
  loadingLabel?: string
  children: (data: T) => ReactNode
}

/**
 * Renders the loading / error / empty / data states of a query.
 * If a reload fails while older data is on screen, the error shows above the old data.
 */
export function QueryBoundary<T>({ query, isEmpty, empty, loadingLabel, children }: QueryBoundaryProps<T>) {
  const { data, error, loading, reload } = query
  if (data === undefined) {
    if (error) return <ErrorState error={error} onRetry={reload} />
    return <Loading label={loadingLabel} />
  }
  return (
    <div className={loading ? 'refreshing' : undefined} aria-busy={loading}>
      {error ? <ErrorState error={error} onRetry={reload} compact /> : null}
      {isEmpty?.(data) ? (empty ?? <EmptyState title="Nothing here yet." />) : children(data)}
    </div>
  )
}
