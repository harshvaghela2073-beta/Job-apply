import { useMemo, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type { Profile } from '../api/types'
import { ProfileContext } from '../hooks/useProfile'
import { useQuery } from '../hooks/useQuery'

/**
 * Loads the profile once for the whole app. It changes rarely, so it is not refetched on
 * navigation; `reload()` refreshes it, and `replace()` shows the profile a save returned.
 */
export function ProfileProvider({ children }: { children: ReactNode }) {
  const { data, error, loading, reload } = useQuery('profile', (signal) => api.profile(signal))

  // The profile from the latest save, shown until a fetch brings newer data.
  const [saved, setSaved] = useState<Profile | null>(null)
  const [fetched, setFetched] = useState(data)
  if (fetched !== data) {
    setFetched(data)
    setSaved(null)
  }

  const current = saved ?? data
  const value = useMemo(
    () => ({ data: current, error, loading, reload, replace: setSaved }),
    [current, error, loading, reload],
  )
  return <ProfileContext.Provider value={value}>{children}</ProfileContext.Provider>
}
