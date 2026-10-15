export type Theme = 'light' | 'dark'

/** localStorage key; index.html reads the same key to apply the theme before first paint. */
const STORAGE_KEY = 'job-apply-theme'

const darkQuery = () => window.matchMedia('(prefers-color-scheme: dark)')

/** The theme the user picked, or null if they haven't picked one (then the OS decides). */
export function getStoredTheme(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null
  }
}

export function getSystemTheme(): Theme {
  return darkQuery().matches ? 'dark' : 'light'
}

export function setTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // Storage unavailable (private mode, blocked): the theme still applies for this visit.
  }
}

/** Calls `onChange` when the OS theme changes. Returns an unsubscribe function. */
export function onSystemThemeChange(onChange: (theme: Theme) => void): () => void {
  const query = darkQuery()
  const listener = () => onChange(query.matches ? 'dark' : 'light')
  query.addEventListener('change', listener)
  return () => query.removeEventListener('change', listener)
}
