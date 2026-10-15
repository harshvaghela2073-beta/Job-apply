import { API_BASE_URL, errorMessage, NETWORK_ERROR_MESSAGE } from '../api/client'
import { PLATFORMS, type Connection, type ConnectionStatus, type Platform } from '../api/types'
import { shownStatus, useConnections } from '../hooks/useConnections'
import { usePopover } from '../hooks/usePopover'
import {
  CONNECTION_STATUS_LABEL,
  CONNECTION_STATUS_TONE,
  formatDateTime,
  formatUtc,
  PLATFORM_LABEL,
} from '../lib/format'
import { Badge } from './Badge'
import { InlineError } from './DataState'

/** Shown while waiting for a login in the user's own Chrome (the main flow). */
export const CONNECTING_HELP =
  'Log in in the tab that just opened in your Chrome (if you are already logged in there, nothing to do). Then come back to this tab; it connects by itself. If macOS asks to allow access to "Chrome Safe Storage", click Always Allow.'

/** Shown while the fallback login window (the tool's own Chrome window) is open. */
export const WINDOW_CONNECTING_HELP =
  'A separate Chrome window opened on your screen. Log in there with your email and password (Google sign-in may be blocked in automated windows). This updates by itself.'

/** Spinner plus the login instructions, shown while a platform is connecting. */
export function ConnectingNote({ platform, compact = false }: { platform?: Platform; compact?: boolean }) {
  const { waiting } = useConnections()
  const viaChrome = platform === undefined ? Object.keys(waiting).length > 0 : waiting[platform] !== undefined
  return (
    <p className={`connecting-note${compact ? ' connecting-note-compact' : ''}`} role="status">
      <span className="spinner" aria-hidden="true" />
      <span>
        {platform && <strong>{PLATFORM_LABEL[platform]}: </strong>}
        {compact ? 'waiting for you to log in…' : viaChrome ? CONNECTING_HELP : WINDOW_CONNECTING_HELP}
      </span>
    </p>
  )
}

/** Fallback for when copying the login from Chrome doesn't work. */
function WindowFallback({ platform, onDone }: { platform: Platform; onDone?: () => void }) {
  const { busy, connectWindow } = useConnections()
  return (
    <button
      type="button"
      className="link-btn small"
      disabled={busy[platform] !== undefined}
      onClick={async () => {
        await connectWindow(platform)
        onDone?.()
      }}
    >
      Not working? Log in in a separate window instead
    </button>
  )
}

interface ConnectButtonProps {
  platform: Platform
  /** Defaults to "Connect <Platform>". */
  label?: string
  className?: string
  /** Called after the request settles (e.g. to move focus somewhere stable). */
  onDone?: () => void
}

/** Opens the platform's login in a new tab of the user's browser and connects once they're logged in. */
export function ConnectButton({ platform, label, className = 'btn btn-sm btn-primary', onDone }: ConnectButtonProps) {
  const { busy, connect } = useConnections()
  const pending = busy[platform] !== undefined
  return (
    <button
      type="button"
      className={className}
      disabled={pending}
      onClick={async () => {
        await connect(platform)
        onDone?.()
      }}
    >
      {busy[platform] === 'connect' ? 'Opening…' : (label ?? `Connect ${PLATFORM_LABEL[platform]}`)}
    </button>
  )
}

/** Dot colour class for a status; `checking` before the first answer arrives. */
type DotState = ConnectionStatus | 'checking' | 'offline'

export function StatusDot({ state }: { state: DotState }) {
  return <span className={`conn-dot conn-dot-${state}`} aria-hidden="true" />
}

function ConnectionDetails({ conn, platform, onSettled }: { conn: Connection | undefined; platform: Platform; onSettled: () => void }) {
  const { busy, errors, waiting, importUnavailable, disconnect, cancelConnect, clearError } = useConnections()
  const status = shownStatus(conn, busy[platform])

  const onDisconnect = async () => {
    const ok = window.confirm(
      `Disconnect ${PLATFORM_LABEL[platform]}?\n\nThis deletes the saved login. Scans that need it will be skipped until you log in again.`,
    )
    if (!ok) return
    await disconnect(platform)
    onSettled()
  }

  return (
    <>
      <div className="conn-pop-head">
        <strong>{PLATFORM_LABEL[platform]}</strong>
        {status ? (
          <Badge tone={CONNECTION_STATUS_TONE[status]}>{CONNECTION_STATUS_LABEL[status]}</Badge>
        ) : (
          <Badge tone="muted">Checking…</Badge>
        )}
      </div>

      <dl className="pop-facts">
        <div>
          <dt>Account</dt>
          <dd>{conn?.account_name || <span className="muted">{status === 'connected' ? 'Name not shown' : '—'}</span>}</dd>
        </div>
        <div>
          <dt>Last checked</dt>
          <dd>
            {conn?.checked_at ? (
              <time dateTime={conn.checked_at} title={formatUtc(conn.checked_at)}>
                {formatDateTime(conn.checked_at)}
              </time>
            ) : (
              <span className="muted">Not checked yet</span>
            )}
          </dd>
        </div>
      </dl>

      {conn?.message && status !== 'connecting' && (
        <p className={`pop-note${status === 'error' ? ' pop-note-error' : ''}`}>{conn.message}</p>
      )}
      {status === 'connecting' && <ConnectingNote platform={platform} />}
      <InlineError message={errors[platform] ?? null} onDismiss={() => clearError(platform)} />

      {status === 'connecting' && waiting[platform] !== undefined && (
        <div className="btn-row">
          <button type="button" className="btn btn-sm" onClick={() => cancelConnect(platform)}>
            Cancel
          </button>
          <WindowFallback platform={platform} onDone={onSettled} />
        </div>
      )}
      {(status === 'disconnected' || status === 'error') && (
        <div className="btn-row">
          <ConnectButton platform={platform} label="Connect" className="btn btn-primary" onDone={onSettled} />
          {!importUnavailable && <WindowFallback platform={platform} onDone={onSettled} />}
        </div>
      )}
      {status === 'connected' && (
        <div className="btn-row">
          <button type="button" className="btn btn-danger-ghost" disabled={busy[platform] !== undefined} onClick={onDisconnect}>
            {busy[platform] === 'disconnect' ? 'Disconnecting…' : 'Disconnect'}
          </button>
        </div>
      )}
    </>
  )
}

/** "● LinkedIn Connected" button at the top right; opens a card with the login details and actions. */
function ConnectionPill({ platform }: { platform: Platform }) {
  const { byPlatform, busy } = useConnections()
  const { open, setOpen, close, focusPopover, wrapRef, buttonRef, popRef, popId, onBlur } = usePopover()
  const conn = byPlatform[platform]
  const status = shownStatus(conn, busy[platform])
  const text = status ? CONNECTION_STATUS_LABEL[status] : 'Checking…'
  const label = `${PLATFORM_LABEL[platform]}: ${text}`

  return (
    <div className="conn-menu" ref={wrapRef} onBlur={onBlur}>
      <button
        ref={buttonRef}
        type="button"
        className={`conn-pill conn-${status ?? 'checking'}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        aria-label={label}
        title={conn?.message ? `${label} — ${conn.message}` : label}
        onClick={() => (open ? close(true) : setOpen(true))}
      >
        <StatusDot state={status ?? 'checking'} />
        <span className="conn-name">{PLATFORM_LABEL[platform]}</span>
        <span className="conn-state">{text}</span>
      </button>

      {open && (
        <div
          ref={popRef}
          id={popId}
          className="popover conn-pop"
          role="dialog"
          aria-label={`${PLATFORM_LABEL[platform]} login`}
          tabIndex={-1}
        >
          <ConnectionDetails conn={conn} platform={platform} onSettled={focusPopover} />
        </div>
      )}
    </div>
  )
}

/** One red pill instead of the platform pills when the backend can't be reached (or fails). */
function BackendPill() {
  const { error, offline, query } = useConnections()
  const { open, setOpen, close, wrapRef, buttonRef, popRef, popId, onBlur } = usePopover()
  const text = offline ? 'Backend offline' : 'Backend error'

  return (
    <div className="conn-menu" ref={wrapRef} onBlur={onBlur}>
      <button
        ref={buttonRef}
        type="button"
        className="conn-pill conn-offline"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        title={`API: ${API_BASE_URL}`}
        onClick={() => (open ? close(true) : setOpen(true))}
      >
        <StatusDot state="offline" />
        <span className="conn-name">{text}</span>
      </button>

      {open && (
        <div ref={popRef} id={popId} className="popover conn-pop" role="dialog" aria-label={text} tabIndex={-1}>
          <p className="pop-note pop-note-error" role="alert">
            <strong>{text}.</strong> {offline ? NETWORK_ERROR_MESSAGE : errorMessage(error)}
          </p>
          <p className="pop-note small">The app checks again every 10 seconds.</p>
          <div className="btn-row">
            <button type="button" className="btn btn-sm" disabled={query.loading} onClick={query.reload}>
              {query.loading ? 'Checking…' : 'Retry now'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

/** Top-bar login status: one pill per platform, or one red pill when the backend is down. */
export function ConnectionPills() {
  const { query, error, offline } = useConnections()
  if (offline || (!query.data && error)) return <BackendPill />
  return (
    <div className="conn-pills" role="group" aria-label="Platform logins">
      {PLATFORMS.map((p) => (
        <ConnectionPill key={p} platform={p} />
      ))}
    </div>
  )
}
