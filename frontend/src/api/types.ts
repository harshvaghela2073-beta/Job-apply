// Types mirroring docs/API_CONTRACT.md (Phase 1) of the backend.
// Keep this file in sync with the contract: enums first, then models, then request bodies.

// ---------- Enums ----------

export const PLATFORMS = ['linkedin', 'naukri'] as const
export type Platform = (typeof PLATFORMS)[number]

export const APPLY_TYPES = ['easy_apply', 'one_click', 'company_site', 'google_form', 'email'] as const
export type ApplyType = (typeof APPLY_TYPES)[number]

export const JOB_STATUSES = ['found', 'queued', 'applied', 'needs_manual', 'failed', 'skipped'] as const
export type JobStatus = (typeof JOB_STATUSES)[number]

export const OUTREACH_KINDS = ['email', 'connection_note', 'inmail'] as const
export type OutreachKind = (typeof OUTREACH_KINDS)[number]

export const OUTREACH_STATUSES = ['draft', 'approved', 'sent', 'rejected', 'failed'] as const
export type OutreachStatus = (typeof OUTREACH_STATUSES)[number]

/** In the order the backend sorts templates: answers, then LinkedIn messages, then emails. */
export const TEMPLATE_KINDS = ['answer', 'linkedin_message', 'email'] as const
export type TemplateKind = (typeof TEMPLATE_KINDS)[number]

export const CONNECTION_STATUSES = ['connected', 'disconnected', 'connecting', 'error'] as const
export type ConnectionStatus = (typeof CONNECTION_STATUSES)[number]

/** In the order GET /api/scans/status returns `latest`. */
export const SCAN_KINDS = [
  'linkedin_jobs',
  'linkedin_apply',
  'linkedin_posts',
  'email_send',
  'linkedin_outreach',
  'naukri_jobs',
  'naukri_apply',
] as const
export type ScanKind = (typeof SCAN_KINDS)[number]

/** The scanners of each scan group, in the order they run. The two groups run independently. */
export const SCAN_KINDS_BY_PLATFORM: Record<Platform, readonly ScanKind[]> = {
  linkedin: ['linkedin_jobs', 'linkedin_apply', 'linkedin_posts', 'email_send', 'linkedin_outreach'],
  naukri: ['naukri_jobs', 'naukri_apply'],
}

export const SCAN_RUN_STATUSES = ['running', 'ok', 'skipped', 'failed'] as const
export type ScanRunStatus = (typeof SCAN_RUN_STATUSES)[number]

/** Statuses a client may set through PATCH /api/jobs/{id}. `found` is shown as Pending. */
export type JobStatusUpdateValue = Extract<JobStatus, 'applied' | 'found' | 'skipped' | 'needs_manual'>

/** Order of GET /api/jobs: newest `posted_at` first (the default) or newest `found_at` first. */
export const JOB_SORTS = ['posted', 'found'] as const
export type JobSort = (typeof JOB_SORTS)[number]

/** Max length of a LinkedIn connection-note body (backend returns 422 above this). */
export const CONNECTION_NOTE_MAX = 300

// ---------- Models ----------

export interface ListResponse<T> {
  items: T[]
  total: number
}

// The contract shows nullability only through its examples. Fields that a source may not
// always know (location, url, short_description) are typed as nullable to be safe.
export interface Job {
  id: number
  platform: Platform
  external_id: string
  title: string
  company: string
  location: string | null
  url: string | null
  apply_url: string | null
  form_url: string | null
  contact_email: string | null
  post_url: string | null
  short_description: string | null
  apply_type: ApplyType
  status: JobStatus
  status_reason: string | null
  found_at: string
  applied_at: string | null
  updated_at: string
  // Filled by the scanner; null for older rows.
  /** When the platform says the job or post was published (estimated from "3 hours ago" labels). */
  posted_at: string | null
  /** Experience range as the platform shows it, e.g. "1-4 Yrs". */
  experience: string | null
  /** The role from `search_roles` that the title matched. */
  matched_role: string | null
  /** Sort key, lower is more important. */
  priority: number | null
}

/** Login state of one platform. There is always exactly one per platform. */
export interface Connection {
  platform: Platform
  status: ConnectionStatus
  /** null when it can't be read. */
  account_name: string | null
  /** Typed nullable: there may be no check yet right after startup. */
  checked_at: string | null
  /** Why the status is `error`, or why a login attempt ended (window closed, timed out). */
  message: string | null
  /** The platform's login page, opened in the user's own browser (optional for older backends). */
  login_url?: string
}

/** One run of one scanner. */
export interface ScanRun {
  id: number
  kind: ScanKind
  started_at: string
  /** null while running. */
  finished_at: string | null
  status: ScanRunStatus
  /** Matching items seen today. */
  found: number
  /** Items not seen before. */
  new: number
  /** Says why a run was skipped or failed. */
  message: string | null
}

/** Schedule of one scan group (one platform). */
export interface PlatformScanStatus {
  running: boolean
  interval_minutes: number
  /** null while the group runs or when scanning is disabled. */
  next_run_at: string | null
  /** null when the group has never finished a run. */
  last_finished_at: string | null
}

export interface ScanStatus {
  enabled: boolean
  /** LinkedIn's scan interval (kept for older UIs). */
  interval_minutes: number
  /** Any group is running. */
  running: boolean
  /** The earliest next run of the groups; null when both run or scanning is disabled. */
  next_run_at: string | null
  /** Per scan group (missing on older backends, which run both platforms together). */
  platforms?: Record<Platform, PlatformScanStatus>
  /**
   * Most recent run of each kind, in SCAN_KINDS order. A kind that has never run may be missing
   * or null, so look runs up by `kind` instead of by position.
   */
  latest: (ScanRun | null)[]
}

export interface ScanStarted {
  started: true
  /** The groups that were started (missing on older backends). */
  platforms?: Platform[]
}

export interface OutreachMessage {
  id: number
  kind: OutreachKind
  job_id: number | null
  recipient_name: string | null
  recipient_profile_url: string | null
  /** Used only when kind is `email`. */
  recipient_email: string | null
  /** Used only when kind is `email`. */
  subject: string | null
  body: string
  status: OutreachStatus
  error: string | null
  created_at: string
  approved_at: string | null
  sent_at: string | null
}

export interface UnansweredQuestion {
  id: number
  question: string
  platform: Platform | null
  job_id: number | null
  seen_at: string
  answered_at: string | null
}

export interface Template {
  id: number
  kind: TemplateKind
  /** The application question for `answer` templates; a short name for messages and emails. */
  question: string
  /** The answer text or message body. Line breaks are kept. */
  answer: string
  /** Used by `email` templates and by `linkedin_message` templates meant as InMails; null otherwise. */
  subject: string | null
  /** False when `answer` is empty or `TBD`. */
  is_answered: boolean
  created_at: string
  updated_at: string
}

export interface CapUsage {
  used: number
  limit: number
}

export interface DashboardCaps {
  easy_apply_today: CapUsage
  naukri_apply_today: CapUsage
  connection_notes_month: CapUsage
  inmail_month: CapUsage
}

export interface DashboardSummary {
  applied_today: number
  applied_total: number
  applied_by_platform: Record<Platform, number>
  /** Jobs with status `needs_manual` or `found`, per apply type. All five apply types are present. */
  needs_action: Record<ApplyType, number>
  /** Outreach messages with status `draft`. */
  outreach_pending: number
  unanswered_questions: number
  caps: DashboardCaps
}

/** Jobs of one platform and apply type. `pending` is `found - applied`. */
export interface ApplyCounts {
  found: number
  /** For `email`: emailed. */
  applied: number
  pending: number
}

export interface LinkedInAnalytics {
  easy_apply: ApplyCounts
  company_site: ApplyCounts
  google_form: ApplyCounts
  email: ApplyCounts
  /** Outreach messages of kind `connection_note` with status `sent`. */
  connection_notes_sent: number
}

export interface NaukriAnalytics {
  one_click: ApplyCounts
  company_site: ApplyCounts
}

export interface AnalyticsPeriod {
  linkedin: LinkedInAnalytics
  naukri: NaukriAnalytics
}

/** GET /api/analytics: `today` counts jobs found today (the server's local day), `all` all time. */
export interface Analytics {
  today: AnalyticsPeriod
  all: AnalyticsPeriod
}

export interface HealthResponse {
  status: 'ok'
}

/** GET /api/auth/status (always public). `required` is false when the backend has no APP_PASSWORD. */
export interface AuthStatus {
  required: boolean
  authenticated: boolean
}

/** POST /api/auth/login: a signed token, valid until `expires_at` (30 days). */
export interface LoginResponse {
  token: string
  expires_at: string
}

// ---------- Request bodies ----------

/** Body of POST /api/auth/login. */
export interface LoginInput {
  password: string
}

export interface JobStatusUpdate {
  status: JobStatusUpdateValue
  status_reason?: string
}

export interface OutreachUpdate {
  subject?: string
  body?: string
}

/** Body of POST /api/templates and PUT /api/templates/{id}. */
export interface TemplateInput {
  /** Defaults to `answer` on create; left out on update, it keeps its current value. */
  kind?: TemplateKind
  question: string
  answer: string
  subject?: string | null
}

export interface AnswerInput {
  answer: string
}

// ---------- Profile (stored in the database as ProfileField rows) ----------

/** One detail about the candidate. */
export interface ProfileField {
  /** Unique, stable, snake_case. */
  key: string
  label: string
  /** Groups fields on the Profile page. */
  section: string
  value: string
  position: number
}

export interface ProfileSection {
  title: string
  /** Sorted by `position`. */
  fields: ProfileField[]
}

export interface ProfileResume {
  filename: string
  /** Relative to the API base URL; turn it into a link with `absoluteApiUrl`. */
  url: string
}

/** Values of the fields with special keys; each is null when that field is missing or empty. */
export interface ProfileSummary {
  name: string | null
  email: string | null
  phone: string | null
  location: string | null
  current_company: string | null
  designation: string | null
  total_experience: string | null
  current_ctc: string | null
  graduation: string | null
  linkedin_url: string | null
  naukri_url: string | null
  /** The `skills` field split on commas, trimmed, empty items removed. */
  skills: string[]
  /** null when the resume file doesn't exist on the server. */
  resume: ProfileResume | null
}

export interface Profile {
  summary: ProfileSummary
  /** In order of each section's lowest field position. */
  sections: ProfileSection[]
}

export interface ProfileFieldInput {
  key: string
  label: string
  section: string
  value: string
  /** Left out: keeps the current position (a new field goes last). */
  position?: number
}

export interface ProfileFieldsUpdate {
  fields: ProfileFieldInput[]
}

// ---------- Query params ----------

export interface Paging {
  limit?: number
  offset?: number
}

export interface ApplicationsQuery extends Paging {
  platform?: Platform
}

export interface JobsQuery extends Paging {
  platform?: Platform
  apply_type?: ApplyType
  /** Repeated as `status=a&status=b`. */
  status?: JobStatus[]
  /** ISO datetime: only jobs found at or after it. */
  found_after?: string
  /** Defaults to `posted` on the backend. */
  sort?: JobSort
}

export interface OutreachQuery extends Paging {
  kind?: OutreachKind
  status?: OutreachStatus
}

/** GET/PUT /api/settings: switches the user can flip in the app. */
export interface AppSettings {
  /** Apply automatically to Naukri one-click jobs after each scan. */
  auto_apply_naukri: boolean
  /** Apply automatically to LinkedIn Easy Apply jobs (missing on older backends). */
  auto_apply_linkedin?: boolean
  /** Email hiring posts automatically, with the resume attached. */
  auto_send_email?: boolean
  /** Send LinkedIn connection notes to hiring-team members and post authors automatically. */
  auto_send_linkedin_notes?: boolean
  /** Pauses `naukri_jobs` and `naukri_apply` (missing on older backends). */
  pause_naukri?: boolean
  /** Pauses `naukri_jobs`, the Naukri search. The pause flags below are missing on older backends. */
  pause_naukri_jobs?: boolean
  /** Pauses every LinkedIn scanner. */
  pause_linkedin?: boolean
  /** Pauses `linkedin_jobs`, the LinkedIn job search. */
  pause_linkedin_jobs?: boolean
  /** Pauses `linkedin_posts`, hiring posts with a Google Form or email. */
  pause_linkedin_posts?: boolean
  /** Minutes between LinkedIn scan runs, 10–240. The intervals are missing on older backends. */
  linkedin_scan_interval_minutes?: number
  /** Minutes between Naukri scan runs, 10–240. */
  naukri_scan_interval_minutes?: number
  /** Minutes between LinkedIn's job and post searches inside LinkedIn runs, 30–720. */
  linkedin_search_interval_minutes?: number
}

/** The on/off settings. */
export type SettingKey = {
  [K in keyof AppSettings]-?: NonNullable<AppSettings[K]> extends boolean ? K : never
}[keyof AppSettings]

/** The scan intervals, in minutes. */
export type IntervalKey =
  | 'linkedin_scan_interval_minutes'
  | 'naukri_scan_interval_minutes'
  | 'linkedin_search_interval_minutes'

/** What PUT /api/settings accepts for each interval. */
export const INTERVAL_RANGE: Record<IntervalKey, readonly [number, number]> = {
  linkedin_scan_interval_minutes: [10, 240],
  naukri_scan_interval_minutes: [10, 240],
  linkedin_search_interval_minutes: [30, 720],
}

/** The setting holding each platform's scan interval. */
export const SCAN_INTERVAL_SETTING: Record<Platform, IntervalKey> = {
  linkedin: 'linkedin_scan_interval_minutes',
  naukri: 'naukri_scan_interval_minutes',
}

/** POST /api/jobs/{id}/apply: what happened on the job page. */
export interface ApplyAttempt {
  result:
    | 'applied'
    | 'already_applied'
    | 'questionnaire'
    | 'company_site'
    | 'quota_reached'
    | 'no_button'
    | 'no_confirmation'
    | 'would_apply'
    | 'skipped'
  detail: string
  questions: string[]
  /** What the job page showed (company, salary, skills, and for LinkedIn the form fields). */
  facts: Record<string, unknown>
  job: Job
}
