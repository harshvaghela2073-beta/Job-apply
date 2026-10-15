import { createContext, useContext } from 'react'
import type { Connection, ListResponse, Platform } from '../api/types'
import type { QueryResult } from './useQuery'

export type ConnectionAction = 'connect' | 'disconnect'

export interface ConnectionsValue {
  /** GET /api/connections, polled: every 3 s while a platform is connecting, otherwise every 60 s. */
  query: QueryResult<ListResponse<Connection>>
  /**
   * Latest known state per platform (includes the answer of a connect/disconnect just made).
   * While the app waits for a login in the user's Chrome, the platform shows as `connecting`.
   */
  byPlatform: Record<Platform, Connection | undefined>
  /** Last settled error of GET /api/connections (kept while a retry is in flight). */
  error: unknown
  /** The backend can't be reached at all. */
  offline: boolean
  /**
   * POST .../import answered 409: the backend has no Chrome to copy logins from (it runs on a
   * server; logins are pushed from the Mac). Connect then only shows that message, and the
   * separate-window fallback is hidden (that window would open on the server too).
   */
  importUnavailable: boolean
  /** A connect/disconnect request in flight, per platform. */
  busy: Partial<Record<Platform, ConnectionAction>>
  /** Platforms waiting for a login in the user's own Chrome (value: when the wait started). */
  waiting: Partial<Record<Platform, number>>
  /** Error of the last connect/disconnect, per platform. */
  errors: Partial<Record<Platform, string>>
  /**
   * Main way to connect. Must be called straight from a click (it opens a tab): opens the login page
   * in a new tab of the user's browser, then keeps asking the backend to copy the login from the
   * user's Chrome until it is connected, the backend reports an error, or 10 minutes pass.
   */
  connect: (platform: Platform) => Promise<boolean>
  /** Fallback: POST .../connect, which opens the tool's own login window on the user's screen. */
  connectWindow: (platform: Platform) => Promise<boolean>
  /** Stops waiting for a login in the user's Chrome. */
  cancelConnect: (platform: Platform) => void
  /** POST .../disconnect. Resolves to true on success. */
  disconnect: (platform: Platform) => Promise<boolean>
  clearError: (platform: Platform) => void
}

export const ConnectionsContext = createContext<ConnectionsValue | null>(null)

export function useConnections(): ConnectionsValue {
  const value = useContext(ConnectionsContext)
  if (!value) throw new Error('useConnections must be used inside <ConnectionsProvider>')
  return value
}

/** Status to show: a connect request still in flight already counts as connecting. */
export function shownStatus(conn: Connection | undefined, busy: ConnectionAction | undefined) {
  if (busy === 'connect') return 'connecting' as const
  return conn?.status
}
