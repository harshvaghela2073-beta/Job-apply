import { createContext, useContext } from 'react'

export interface AuthValue {
  /** The backend asks for the app password (APP_PASSWORD is set), so "Log out" makes sense. */
  required: boolean
  /** Drops the token and shows the login screen. */
  logout: () => void
}

/** App login state, provided by <AuthGate> around the app. */
export const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthGate>')
  return value
}
