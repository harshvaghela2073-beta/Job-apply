import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, errorMessage } from '../api/client'
import { Wordmark } from './Wordmark'

function loginError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Wrong password.'
    if (err.status === 429) return 'Too many attempts. Try again later.'
  }
  return errorMessage(err)
}

/** The app password screen: wordmark, one password field, "Log in". Calls `onLogin` with the new token. */
export function LoginScreen({ onLogin }: { onLogin: (token: string) => void }) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const errorId = useId()

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (busy) return
    if (!password) {
      inputRef.current?.focus()
      return
    }
    setBusy(true)
    setError(null)
    try {
      const { token } = await api.login({ password })
      onLogin(token)
    } catch (err) {
      setError(loginError(err))
      setBusy(false)
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }

  return (
    <main className="auth-screen">
      <form className="login" onSubmit={submit}>
        <h1 className="login-brand">
          <Wordmark />
          <span className="sr-only">Job Hunt</span>
        </h1>
        <label className="field">
          <span>Password</span>
          <input
            ref={inputRef}
            type="password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            readOnly={busy}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : undefined}
          />
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy && <span className="spinner spinner-sm spinner-on-accent" aria-hidden="true" />}
          {busy ? 'Logging in…' : 'Log in'}
        </button>
        {error && (
          <p id={errorId} className="inline-error" role="alert">
            {error}
          </p>
        )}
      </form>
    </main>
  )
}
