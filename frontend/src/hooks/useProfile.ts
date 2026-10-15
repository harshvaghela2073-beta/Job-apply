import { createContext, useContext } from 'react'
import type { Profile } from '../api/types'
import type { QueryResult } from './useQuery'

export interface ProfileState extends QueryResult<Profile> {
  /** Shows the profile a save returned (PUT /api/profile/fields returns the full profile). */
  replace: (profile: Profile) => void
}

/** GET /api/profile, loaded once and shared by the top-bar avatar, the Profile page and the Template Library. */
export const ProfileContext = createContext<ProfileState | null>(null)

export function useProfile(): ProfileState {
  const value = useContext(ProfileContext)
  if (!value) throw new Error('useProfile must be used inside <ProfileProvider>')
  return value
}
