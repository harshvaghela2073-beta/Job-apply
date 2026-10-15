import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, ApiError, getToken, onUnauthorized, setToken } from '../api/client'
import { AuthContext, type AuthValue } from '../hooks/useAuth'
import { Loading } from './DataState'
import { LoginScreen } from './LoginScreen'

type GateState = { view: 'checking' } | { view: 'login' } | { view: 'app'; required: boolean }

/**
 * App login. Asks GET /api/auth/status first: without a login on the backend (local use) the app
 * renders straight away; otherwise it shows the password screen until a login succeeds. Any 401
 * later (token expired, password changed) brings the password screen back; after the next login
 * the app mounts again, so every query refetches.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>({ view: 'checking' })

  useEffect(() => {
    const controller = new AbortController()
    api.authStatus(controller.signal).then(
      (status) => {
        if (controller.signal.aborted) return
        if (status.required && !status.authenticated) {
          setToken(null) // expired, or signed with an older password
          setState({ view: 'login' })
        } else {
          setState({ view: 'app', required: status.required })
        }
      },
      (err: unknown) => {
        if (controller.signal.aborted) return
        // 404: an older backend without login. Anything else (backend down, server error): show the
        // app, which reports the problem itself; a 401 from any later call still shows the login.
        const noLogin = err instanceof ApiError && err.status === 404
        setState({ view: 'app', required: !noLogin && getToken() !== null })
      },
    )
    return () => controller.abort()
  }, [])

  useEffect(() => onUnauthorized(() => setState({ view: 'login' })), [])

  const logout = useCallback(() => {
    setToken(null)
    setState({ view: 'login' })
  }, [])

  const onLogin = useCallback((token: string) => {
    setToken(token)
    setState({ view: 'app', required: true })
  }, [])

  const required = state.view === 'app' && state.required
  const value = useMemo<AuthValue>(() => ({ required, logout }), [required, logout])

  if (state.view === 'checking') {
    return (
      <main className="auth-screen auth-checking">
        <Loading />
      </main>
    )
  }
  if (state.view === 'login') return <LoginScreen onLogin={onLogin} />
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
