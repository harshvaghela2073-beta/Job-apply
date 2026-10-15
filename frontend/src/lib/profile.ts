import type { Profile, ProfileField, ProfileSummary } from '../api/types'

const words = (name: string) => name.trim().split(/\s+/).filter(Boolean)

/** "Harsh Vaghela" -> "HV". "?" when there is no name. */
export function initials(name: string | null | undefined): string {
  const parts = name ? words(name) : []
  if (parts.length === 0) return '?'
  const first = [...parts[0]][0] ?? ''
  const last = parts.length > 1 ? ([...parts[parts.length - 1]][0] ?? '') : ''
  return (first + last).toUpperCase() || '?'
}

export function firstName(name: string | null | undefined): string | null {
  return (name && words(name)[0]) || null
}

export const hasValue = (value: string | null | undefined): value is string => !!value && value.trim() !== ''

/** True when the summary has no details at all (a resume on its own doesn't count). */
export function isEmptySummary(s: ProfileSummary): boolean {
  return (
    s.skills.length === 0 &&
    [
      s.name,
      s.email,
      s.phone,
      s.location,
      s.current_company,
      s.designation,
      s.total_experience,
      s.current_ctc,
      s.graduation,
      s.linkedin_url,
      s.naukri_url,
    ].every((v) => !hasValue(v))
  )
}

export function isEmptyProfile(p: Profile): boolean {
  return isEmptySummary(p.summary) && p.sections.every((s) => s.fields.every((f) => !hasValue(f.value)))
}

/** "https://www.linkedin.com/in/someone/" -> "linkedin.com/in/someone". */
export function displayUrl(url: string): string {
  return url.replace(/^[a-z]+:\/\//i, '').replace(/^www\./i, '').replace(/\/+$/, '')
}

/** `tel:` link for a phone number as typed ("+91 90000 00001" -> "tel:+919000000001"). */
export function telHref(phone: string): string {
  return `tel:${phone.replace(/[^\d+]/g, '')}`
}

/** "1.9+ years" -> "1.9+ years experience"; leaves values that already say so alone. */
export function experienceText(value: string): string {
  return /experience/i.test(value) ? value : `${value} experience`
}

/** Joins the parts that are present with " · ". */
export function joinDot(...parts: (string | null | undefined)[]): string {
  return parts.filter(hasValue).join(' · ')
}

/** The profile field with this key, or null when there is none. */
export function findField(profile: Profile, key: string): ProfileField | null {
  for (const section of profile.sections) {
    const field = section.fields.find((f) => f.key === key)
    if (field) return field
  }
  return null
}

/** Value of the profile field with this key, or null when it is missing or empty. */
export function fieldValue(profile: Profile, key: string): string | null {
  const value = findField(profile, key)?.value
  return hasValue(value) ? value : null
}

/** "a, b ,, c" -> ["a", "b", "c"] (order kept: these lists are priorities). */
export function splitList(value: string | null): string[] {
  return (value ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

/** Stored form of a list field: ["a", "b"] -> "a, b". */
export const joinList = (items: string[]) => items.join(', ')

/** A value that is one http(s) URL and nothing else. */
export const isUrl = (value: string) => /^https?:\/\/\S+$/i.test(value.trim())

/** A value that is one email address and nothing else. */
export const isEmail = (value: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
