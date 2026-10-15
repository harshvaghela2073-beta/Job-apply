import { useState } from 'react'
import { PLATFORMS } from '../api/types'
import { shownStatus, useConnections } from '../hooks/useConnections'
import { PLATFORM_LABEL } from '../lib/format'
import { ConnectButton, CONNECTING_HELP, StatusDot, WINDOW_CONNECTING_HELP } from './Connections'
import { InlineError } from './DataState'

/** sessionStorage key: the banner stays hidden for the rest of this browser session once dismissed. */
const DISMISS_KEY = 'job-apply-connect-banner-dismissed'

function readDismissed(): boolean {
  try {
    return sessionStorage.getItem(DISMISS_KEY) === '1'
  } catch {
    return false
  }
}

function storeDismissed() {
  try {
    sessionStorage.setItem(DISMISS_KEY, '1')
  } catch {
    // Storage unavailable (private mode, blocked): it stays hidden until the page reloads.
  }
}

/** Shown under the top bar while any platform is not logged in, with a Connect button per platform. */
export function ConnectBanner() {
  const { byPlatform, busy, errors, offline, waiting, clearError } = useConnections()
  const [dismissed, setDismissed] = useState(readDismissed)

  if (dismissed || offline) return null
  const pending = PLATFORMS.filter((p) => {
    const status = shownStatus(byPlatform[p], busy[p])
    return status !== undefined && status !== 'connected'
  })
  if (pending.length === 0) return null

  const anyConnecting = pending.some((p) => shownStatus(byPlatform[p], busy[p]) === 'connecting')
  const anyViaChrome = pending.some((p) => waiting[p] !== undefined)
  const names = pending.map((p) => PLATFORM_LABEL[p]).join(' and ')

  return (
    <section className="connect-banner" aria-label="Log in to the job portals">
      <div className="connect-banner-inner">
        <div className="connect-banner-main">
          <p className="connect-banner-text">
            <strong>Log in to {names} to start the scans.</strong>
          </p>
          <div className="connect-banner-actions">
            {pending.map((p) => {
              const status = shownStatus(byPlatform[p], busy[p])
              return status === 'connecting' ? (
                <span key={p} className="conn-inline" role="status">
                  <span className="spinner" aria-hidden="true" />
                  {PLATFORM_LABEL[p]}: waiting for you to log in…
                </span>
              ) : (
                <ConnectButton key={p} platform={p} />
              )
            })}
          </div>
          <button
            type="button"
            className="link-btn connect-banner-close"
            aria-label="Hide this message for this session"
            title="Hide for this session"
            onClick={() => {
              storeDismissed()
              setDismissed(true)
            }}
          >
            ×
          </button>
        </div>

        {anyConnecting && <p className="connect-banner-help">{anyViaChrome ? CONNECTING_HELP : WINDOW_CONNECTING_HELP}</p>}
        {pending.map((p) => {
          const conn = byPlatform[p]
          const status = shownStatus(conn, busy[p])
          if (!conn?.message || status === 'connecting') return null
          return (
            <p key={p} className={`connect-banner-note${status === 'error' ? ' is-error' : ''}`}>
              <StatusDot state={status ?? 'checking'} /> {PLATFORM_LABEL[p]}: {conn.message}
            </p>
          )
        })}
        {pending.map((p) => (
          <InlineError key={p} message={errors[p] ? `${PLATFORM_LABEL[p]}: ${errors[p]}` : null} onDismiss={() => clearError(p)} />
        ))}
      </div>
    </section>
  )
}
