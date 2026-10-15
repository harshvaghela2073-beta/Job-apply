import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, ApiError, errorMessage } from '../api/client'
import { PLATFORMS, type Connection, type Platform } from '../api/types'
import { ConnectionsContext, type ConnectionAction, type ConnectionsValue } from '../hooks/useConnections'
import { usePolling } from '../hooks/usePolling'
import { useQuery } from '../hooks/useQuery'
import { PLATFORM_LABEL } from '../lib/format'

const FAST_POLL_MS = 3_000
const OFFLINE_POLL_MS = 10_000
const SLOW_POLL_MS = 60_000
/** How often to ask the backend to copy the login from Chrome while waiting for the user. */
const IMPORT_POLL_MS = 4_000
const IMPORT_TIMEOUT_MS = 10 * 60_000

/** Used when the backend doesn't send `login_url`. */
const DEFAULT_LOGIN_URL: Record<Platform, string> = {
  linkedin: 'https://www.linkedin.com/login',
  naukri: 'https://www.naukri.com/nlogin/login',
}

function without<T>(record: Partial<Record<Platform, T>>, platform: Platform): Partial<Record<Platform, T>> {
  if (!(platform in record)) return record
  const next = { ...record }
  delete next[platform]
  return next
}

const isNetworkError = (err: unknown) => err instanceof ApiError && err.isNetworkError

/**
 * Loads the platform logins once for the whole app (top-bar pills, connect banner, dashboard) and
 * keeps them fresh: every 3 s while a login is in progress, every 10 s while the backend is
 * unreachable, otherwise every 60 s.
 *
 * Connecting: the login page opens in a new tab of the user's own browser; meanwhile the app asks
 * the backend every few seconds (and whenever this tab gets focus back) to copy the login from the
 * user's Chrome, until it reports `connected`.
 */
export function ConnectionsProvider({ children }: { children: ReactNode }) {
  const { data, loading, error, reload } = useQuery('connections', (signal) => api.connections(signal))
  const query = useMemo(() => ({ data, loading, error, reload }), [data, loading, error, reload])

  // useQuery clears `error` while a retry is in flight; keep the last settled one so the
  // "Backend offline" state doesn't flicker on every poll.
  const [settledError, setSettledError] = useState<unknown>(undefined)
  if (!loading && error !== settledError) setSettledError(error)
  const offline = isNetworkError(settledError)

  // The answer of a connect/disconnect, shown until the reload that follows it comes back.
  const [overrides, setOverrides] = useState<Partial<Record<Platform, Connection>>>({})
  const [seenData, setSeenData] = useState(data)
  if (!loading && data !== seenData) {
    setSeenData(data)
    if (Object.keys(overrides).length) setOverrides({})
  }

  // Platforms whose login we're waiting for in the user's Chrome -> when the wait started.
  const [waiting, setWaiting] = useState<Partial<Record<Platform, number>>>({})

  const byPlatform = useMemo(() => {
    const result = {} as Record<Platform, Connection | undefined>
    for (const p of PLATFORMS) {
      const conn = overrides[p] ?? data?.items.find((c) => c.platform === p)
      result[p] =
        waiting[p] !== undefined && conn && conn.status !== 'connected' ? { ...conn, status: 'connecting', message: null } : conn
    }
    return result
  }, [data, overrides, waiting])

  // POST .../import answered 409: the backend runs where the user's Chrome isn't (e.g. a server), so
  // logins are pushed from the Mac. Connect then shows that answer without opening the login tab.
  const [noChrome, setNoChrome] = useState<string | null>(null)

  const [busy, setBusy] = useState<Partial<Record<Platform, ConnectionAction>>>({})
  const [errors, setErrors] = useState<Partial<Record<Platform, string>>>({})

  const anyConnecting = PLATFORMS.some((p) => byPlatform[p]?.status === 'connecting' || busy[p] === 'connect')
  usePolling(query, anyConnecting ? FAST_POLL_MS : offline || (!data && settledError) ? OFFLINE_POLL_MS : SLOW_POLL_MS)

  const byPlatformRef = useRef(byPlatform)
  const waitingRef = useRef(waiting)
  const noChromeRef = useRef(noChrome)
  useEffect(() => {
    byPlatformRef.current = byPlatform
    waitingRef.current = waiting
    noChromeRef.current = noChrome
  })

  const stopWaiting = useCallback((platform: Platform) => setWaiting((w) => without(w, platform)), [])

  // One import request per platform at a time (each one may load a page to confirm the login).
  const importing = useRef<Partial<Record<Platform, boolean>>>({})
  const tryImport = useCallback(
    async (platform: Platform) => {
      if (importing.current[platform] || waitingRef.current[platform] === undefined) return
      const started = waitingRef.current[platform] ?? Date.now()
      if (Date.now() - started > IMPORT_TIMEOUT_MS) {
        stopWaiting(platform)
        setErrors((e) => ({
          ...e,
          [platform]: `No ${PLATFORM_LABEL[platform]} login showed up in your Chrome. Log in in the tab that opened, then click Connect again.`,
        }))
        return
      }
      importing.current[platform] = true
      try {
        const conn = await api.importLogin(platform)
        if (waitingRef.current[platform] === undefined) return // cancelled meanwhile
        // Keep waiting only while Chrome has no login yet ("Not logged in to <Platform> in your Chrome
        // yet."). Any other answer is final: a login was found but couldn't be confirmed, and asking
        // again every few seconds would just reload the platform's pages for nothing.
        const stillWaiting = conn.status === 'disconnected' && /^not logged in to /i.test(conn.message ?? '')
        if (!stillWaiting) {
          stopWaiting(platform)
          setOverrides((o) => ({ ...o, [platform]: conn }))
          if (conn.status !== 'connected') {
            setErrors((e) => ({ ...e, [platform]: conn.message ?? 'Could not confirm the login.' }))
          }
          reload()
        }
      } catch (err) {
        if (waitingRef.current[platform] === undefined) return
        // The backend is unreachable or restarting: keep trying until the timeout.
        if (isNetworkError(err)) return
        stopWaiting(platform)
        if (err instanceof ApiError && err.status === 409) {
          // "Chrome isn't on this server: push the login from your Mac."
          noChromeRef.current = err.detail
          setNoChrome(err.detail)
        }
        setErrors((e) => ({ ...e, [platform]: errorMessage(err) }))
      } finally {
        importing.current[platform] = false
      }
    },
    [reload, stopWaiting],
  )

  // While waiting: ask again every few seconds, and as soon as the user comes back to this tab.
  const waitingPlatforms = PLATFORMS.filter((p) => waiting[p] !== undefined)
  const waitingKey = waitingPlatforms.join(',')
  useEffect(() => {
    if (!waitingKey) return
    const platforms = waitingKey.split(',') as Platform[]
    const tick = () => platforms.forEach((p) => void tryImport(p))
    const timer = window.setInterval(tick, IMPORT_POLL_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') tick()
    }
    window.addEventListener('focus', tick)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', tick)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [waitingKey, tryImport])

  const connect = useCallback(
    async (platform: Platform): Promise<boolean> => {
      const noChromeMessage = noChromeRef.current
      if (noChromeMessage) {
        setErrors((e) => ({ ...e, [platform]: noChromeMessage }))
        return false
      }
      // Open the tab first, synchronously inside the click, so the browser doesn't block it.
      const url = byPlatformRef.current[platform]?.login_url || DEFAULT_LOGIN_URL[platform]
      window.open(url, '_blank', 'noopener,noreferrer')
      setErrors((e) => without(e, platform))
      const started = Date.now()
      waitingRef.current = { ...waitingRef.current, [platform]: started }
      setWaiting((w) => ({ ...w, [platform]: started }))
      await tryImport(platform) // already logged in to Chrome -> connects right away
      return true
    },
    [tryImport],
  )

  const cancelConnect = useCallback(
    (platform: Platform) => {
      waitingRef.current = without(waitingRef.current, platform)
      stopWaiting(platform)
    },
    [stopWaiting],
  )

  const runAction = useCallback(
    async (platform: Platform, action: ConnectionAction): Promise<boolean> => {
      setBusy((b) => ({ ...b, [platform]: action }))
      setErrors((e) => without(e, platform))
      if (action === 'disconnect') cancelConnect(platform)
      try {
        const conn = await (action === 'connect' ? api.connect(platform) : api.disconnect(platform))
        setOverrides((o) => ({ ...o, [platform]: conn }))
        return true
      } catch (err) {
        // 409 on connect: a login window for this platform is already open. That's fine.
        const known = byPlatformRef.current[platform]
        if (action === 'connect' && err instanceof ApiError && err.status === 409) {
          if (known) setOverrides((o) => ({ ...o, [platform]: { ...known, status: 'connecting', message: null } }))
          return true
        }
        setErrors((e) => ({ ...e, [platform]: errorMessage(err) }))
        return false
      } finally {
        setBusy((b) => without(b, platform))
        reload()
      }
    },
    [reload, cancelConnect],
  )

  const connectWindow = useCallback(
    (platform: Platform) => {
      cancelConnect(platform)
      return runAction(platform, 'connect')
    },
    [runAction, cancelConnect],
  )
  const disconnect = useCallback((platform: Platform) => runAction(platform, 'disconnect'), [runAction])
  const clearError = useCallback((platform: Platform) => setErrors((e) => without(e, platform)), [])

  const value = useMemo<ConnectionsValue>(
    () => ({
      query,
      byPlatform,
      error: settledError,
      offline,
      importUnavailable: noChrome !== null,
      busy,
      waiting,
      errors,
      connect,
      connectWindow,
      cancelConnect,
      disconnect,
      clearError,
    }),
    [query, byPlatform, settledError, offline, noChrome, busy, waiting, errors, connect, connectWindow, cancelConnect, disconnect, clearError],
  )
  return <ConnectionsContext.Provider value={value}>{children}</ConnectionsContext.Provider>
}
