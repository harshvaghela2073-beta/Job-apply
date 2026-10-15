import type {
  ApplyType,
  ConnectionStatus,
  JobStatus,
  OutreachKind,
  OutreachStatus,
  Platform,
  ScanRunStatus,
  TemplateKind,
} from '../api/types'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * Parses an API datetime. The API sends UTC ISO strings ("...Z"); a string with no timezone
 * designator is treated as UTC too, so it is never misread as local time.
 */
export function parseApiDate(value: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return new Date(hasZone || !value.includes('T') ? value : `${value}Z`)
}

const pad = (n: number) => String(n).padStart(2, '0')

/** "13 Oct 2026, 12:15" in the browser's local time. Empty string for null. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return ''
  const d = parseApiDate(value)
  if (Number.isNaN(d.getTime())) return value
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

/** "14:05" for a time today (local), "12 Oct, 14:05" for any other day. Empty string for null. */
export function formatClock(value: string | null | undefined, now: Date = new Date()): string {
  if (!value) return ''
  const d = parseApiDate(value)
  if (Number.isNaN(d.getTime())) return value
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return sameDay(d, now) ? time : `${d.getDate()} ${MONTHS[d.getMonth()]}, ${time}`
}

/** "just now", "12 min ago", "3 h ago", "2 d ago" (future times read as "in 5 min"). */
export function formatRelative(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return ''
  const t = parseApiDate(value).getTime()
  if (Number.isNaN(t)) return value
  const diff = now - t
  const minutes = Math.round(Math.abs(diff) / 60_000)
  if (minutes < 1) return 'just now'
  const text =
    minutes < 60
      ? `${minutes} min`
      : minutes < 60 * 24
        ? `${Math.round(minutes / 60)} h`
        : `${Math.round(minutes / (60 * 24))} d`
  return diff >= 0 ? `${text} ago` : `in ${text}`
}

/** Full UTC timestamp for tooltips. */
export function formatUtc(value: string | null | undefined): string {
  if (!value) return ''
  const d = parseApiDate(value)
  return Number.isNaN(d.getTime()) ? value : `${d.toISOString().replace('T', ' ').slice(0, 16)} UTC`
}

export const PLATFORM_LABEL: Record<Platform, string> = {
  linkedin: 'LinkedIn',
  naukri: 'Naukri',
}

export const APPLY_TYPE_LABEL: Record<ApplyType, string> = {
  easy_apply: 'Easy Apply',
  one_click: 'One-click',
  company_site: 'Company site',
  google_form: 'Google Form',
  email: 'Email',
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  found: 'Found',
  queued: 'Queued',
  applied: 'Applied',
  needs_manual: 'Needs action',
  failed: 'Failed',
  skipped: 'Skipped',
}

export const OUTREACH_KIND_LABEL: Record<OutreachKind, string> = {
  email: 'Email',
  connection_note: 'Connection note',
  inmail: 'InMail',
}

/** Plural labels, used by the Template Library filter. */
export const TEMPLATE_KIND_LABEL: Record<TemplateKind, string> = {
  answer: 'Application answers',
  linkedin_message: 'LinkedIn messages',
  email: 'Emails',
}

/** Singular labels, used by the template form. */
export const TEMPLATE_KIND_SINGULAR: Record<TemplateKind, string> = {
  answer: 'Application answer',
  linkedin_message: 'LinkedIn message',
  email: 'Email',
}

export const OUTREACH_STATUS_LABEL: Record<OutreachStatus, string> = {
  draft: 'Draft',
  approved: 'Approved',
  sent: 'Sent',
  rejected: 'Rejected',
  failed: 'Failed',
}

export const CONNECTION_STATUS_LABEL: Record<ConnectionStatus, string> = {
  connected: 'Connected',
  disconnected: 'Disconnected',
  connecting: 'Connecting…',
  error: 'Error',
}

export const SCAN_STATUS_LABEL: Record<ScanRunStatus, string> = {
  running: 'Running',
  ok: 'OK',
  skipped: 'Skipped',
  failed: 'Failed',
}

/** Visual tone for a status badge. */
export type Tone = 'neutral' | 'info' | 'good' | 'warning' | 'critical' | 'muted'

export const OUTREACH_STATUS_TONE: Record<OutreachStatus, Tone> = {
  draft: 'warning',
  approved: 'info',
  sent: 'good',
  rejected: 'muted',
  failed: 'critical',
}

export const SCAN_STATUS_TONE: Record<ScanRunStatus, Tone> = {
  running: 'info',
  ok: 'good',
  skipped: 'warning',
  failed: 'critical',
}

export const CONNECTION_STATUS_TONE: Record<ConnectionStatus, Tone> = {
  connected: 'good',
  disconnected: 'muted',
  connecting: 'warning',
  error: 'critical',
}

export function pluralize(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** Length as the backend (Python) counts it: code points, not UTF-16 units. */
export const charCount = (text: string) => [...text].length
