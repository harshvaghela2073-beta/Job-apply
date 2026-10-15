import { useEffect, useState } from 'react'
import { getStoredTheme, getSystemTheme, onSystemThemeChange, setTheme, type Theme } from '../lib/theme'

const OPTIONS: { value: Theme; label: string }[] = [
  { value: 'light', label: 'White' },
  { value: 'dark', label: 'Black' },
]

/** White / Black theme switch. Follows the OS until the user picks one; the pick is remembered. */
export function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(() => getStoredTheme() ?? getSystemTheme())

  useEffect(
    () =>
      onSystemThemeChange((system) => {
        if (!getStoredTheme()) setThemeState(system)
      }),
    [],
  )

  const choose = (value: Theme) => {
    setTheme(value)
    setThemeState(value)
  }

  return (
    <div className="segmented theme-toggle" role="group" aria-label="Theme">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={theme === option.value}
          onClick={() => choose(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
