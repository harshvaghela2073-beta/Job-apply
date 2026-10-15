import type {
  Analytics,
  AppSettings,
  ApplyAttempt,
  AnswerInput,
  ApplicationsQuery,
  AuthStatus,
  Connection,
  DashboardSummary,
  HealthResponse,
  Job,
  JobStatusUpdate,
  JobsQuery,
  ListResponse,
  LoginInput,
  LoginResponse,
  OutreachMessage,
  OutreachQuery,
  OutreachUpdate,
  Paging,
  Platform,
  Profile,
  ProfileFieldsUpdate,
  ScanRun,
  ScanStarted,
  ScanStatus,
  Template,
  TemplateInput,
  TemplateKind,
  UnansweredQuestion,
} from './types'

const DEFAULT_BASE_URL = 'http://localhost:8000'

/** Backend base URL, from VITE_API_BASE_URL at build time. No trailing slash. */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, '')

/**
 * Turns a URL the API returns relative to its base (e.g. the resume's `/api/profile/resume`)
 * into an absolute one the browser can open. Absolute URLs are returned unchanged.
 */
export function absoluteApiUrl(url: string): string {
  if (/^[a-z][a-z\d+.-]*:/i.test(url) || url.startsWith('//')) return url
  return `${API_BASE_URL}${url.startsWith('/') ? '' : '/'}${url}`
}

/** Largest page size the backend accepts on list endpoints. */
export const MAX_LIMIT = 200

/**
 * Error thrown for any failed API call.
 * `status` is the HTTP status, or 0 when the backend could not be reached at all.
 * `detail` is a human-readable message (FastAPI's `detail`, flattened if it was a list).
 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  readonly body: unknown

  constructor(status: number, detail: string, body?: unknown) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.body = body
  }

  get isNetworkError(): boolean {
    return this.status === 0
  }
}

export const NETWORK_ERROR_MESSAGE = `Can't reach the backend at ${API_BASE_URL} — is it running?`

// ---------- App login token ----------

/** localStorage key of the app-login token (see POST /api/auth/login). */
const TOKEN_KEY = 'job-apply-token'

/** Used only when storage can't be written (private mode, blocked): the login then lasts until a reload. */
let memoryToken: string | null = null

/** The app-login token, or null when not logged in (or when the backend has no login). */
export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? memoryToken
  } catch {
    return memoryToken
  }
}

/** Stores the token (null removes it). Every API request then sends it as `Authorization: Bearer`. */
export function setToken(token: string | null) {
  memoryToken = null
  try {
    if (token === null) localStorage.removeItem(TOKEN_KEY)
    else localStorage.setItem(TOKEN_KEY, token)
  } catch {
    memoryToken = token
  }
}

const unauthorizedListeners = new Set<() => void>()

/**
 * Calls `listener` when the backend answers 401 ("Login required.") to a request made with the
 * current token, after the token has been dropped. Returns an unsubscribe function.
 */
export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[]

interface RequestOptions {
  query?: Record<string, QueryValue>
  body?: unknown
  signal?: AbortSignal
  /** `/api/auth/*`: a 401 there is an answer (wrong password), not a sign the login has expired. */
  authEndpoint?: boolean
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const params = new URLSearchParams()
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue
      if (Array.isArray(value)) {
        for (const v of value) params.append(key, String(v))
      } else {
        params.append(key, String(value))
      }
    }
  }
  const qs = params.toString()
  return `${API_BASE_URL}${path}${qs ? `?${qs}` : ''}`
}

/** Turns FastAPI's `detail` (a string, or a list of validation errors) into one message. */
function extractDetail(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (Array.isArray(detail)) {
      const parts = detail.map((d) => {
        if (d && typeof d === 'object' && 'msg' in d) {
          const { msg, loc } = d as { msg: unknown; loc?: unknown }
          const field = Array.isArray(loc) ? loc.filter((l) => l !== 'body').join('.') : ''
          return field ? `${field}: ${String(msg)}` : String(msg)
        }
        return typeof d === 'string' ? d : JSON.stringify(d)
      })
      if (parts.length) return parts.join('; ')
    }
  }
  return fallback
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const { query, body, signal, authEndpoint = false } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  // Every request goes to API_BASE_URL (see buildUrl), so the token is only ever sent to the backend.
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, NETWORK_ERROR_MESSAGE)
  }

  const text = res.status === 204 ? '' : await res.text()
  let parsed: unknown = undefined
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      if (res.ok) throw new ApiError(res.status, `Unexpected non-JSON response from ${path}`, text)
    }
  }

  // Login missing or expired: drop the token and let the app show the login screen. A 401 to a
  // request sent before the token changed (e.g. just before a login) is stale, so it is ignored.
  if (res.status === 401 && !authEndpoint && getToken() === token) {
    setToken(null)
    for (const listener of [...unauthorizedListeners]) listener()
  }

  if (!res.ok) {
    const fallback = `${res.status} ${res.statusText || 'Request failed'}`.trim()
    throw new ApiError(res.status, extractDetail(parsed, fallback), parsed ?? text)
  }
  return parsed as T
}

/** Fetches every page of a list endpoint (used for small lists shown without paging). */
async function fetchAll<T>(
  path: string,
  query: Record<string, QueryValue> = {},
  signal?: AbortSignal,
): Promise<ListResponse<T>> {
  const items: T[] = []
  let total = 0
  for (let offset = 0; ; offset += MAX_LIMIT) {
    const page = await request<ListResponse<T>>('GET', path, {
      query: { ...query, limit: MAX_LIMIT, offset },
      signal,
    })
    items.push(...page.items)
    total = page.total
    if (page.items.length < MAX_LIMIT || items.length >= total) break
  }
  return { items, total }
}

function paging(p: Paging): Record<string, QueryValue> {
  return { limit: p.limit, offset: p.offset }
}

export const api = {
  health: (signal?: AbortSignal) => request<HealthResponse>('GET', '/api/health', { signal }),

  // App login (only when the backend sets APP_PASSWORD)
  authStatus: (signal?: AbortSignal) =>
    request<AuthStatus>('GET', '/api/auth/status', { signal, authEndpoint: true }),
  /** 401: wrong password; 429: too many wrong passwords from this address (wait 15 minutes). */
  login: (body: LoginInput) => request<LoginResponse>('POST', '/api/auth/login', { body, authEndpoint: true }),

  // Dashboard
  summary: (signal?: AbortSignal) => request<DashboardSummary>('GET', '/api/dashboard/summary', { signal }),
  applications: (q: ApplicationsQuery = {}, signal?: AbortSignal) =>
    request<ListResponse<Job>>('GET', '/api/applications', {
      query: { platform: q.platform, ...paging(q) },
      signal,
    }),
  /** Found / applied / pending per platform and apply type, today and all time. */
  analytics: (signal?: AbortSignal) => request<Analytics>('GET', '/api/analytics', { signal }),

  // Jobs
  jobs: (q: JobsQuery = {}, signal?: AbortSignal) =>
    request<ListResponse<Job>>('GET', '/api/jobs', {
      query: {
        platform: q.platform,
        apply_type: q.apply_type,
        status: q.status,
        found_after: q.found_after,
        sort: q.sort,
        ...paging(q),
      },
      signal,
    }),
  job: (id: number, signal?: AbortSignal) => request<Job>('GET', `/api/jobs/${id}`, { signal }),
  updateJob: (id: number, body: JobStatusUpdate) => request<Job>('PATCH', `/api/jobs/${id}`, { body }),

  // Connections (platform logins)
  connections: (signal?: AbortSignal) => request<ListResponse<Connection>>('GET', '/api/connections', { signal }),
  /** Copies the platform's login from the user's own Chrome; returns the resulting status. */
  importLogin: (platform: Platform) => request<Connection>('POST', `/api/connections/${platform}/import`),
  /** Fallback: opens a visible Chrome login window on the server's screen (202, status `connecting`; 409 if one is open). */
  connect: (platform: Platform) => request<Connection>('POST', `/api/connections/${platform}/connect`),
  /** Deletes the platform's saved login; returns it as `disconnected`. */
  disconnect: (platform: Platform) => request<Connection>('POST', `/api/connections/${platform}/disconnect`),

  // Settings (auto-apply and pause switches)
  settings: (signal?: AbortSignal) => request<AppSettings>('GET', '/api/settings', { signal }),
  updateSettings: (body: Partial<AppSettings>) => request<AppSettings>('PUT', '/api/settings', { body }),
  /** Apply to one Naukri one-click job now; `dryRun` only reports the Apply button. */
  applyJob: (id: number, dryRun = false) =>
    request<ApplyAttempt>('POST', `/api/jobs/${id}/apply`, { query: { dry_run: dryRun || undefined } }),

  // Scans
  scanStatus: (signal?: AbortSignal) => request<ScanStatus>('GET', '/api/scans/status', { signal }),
  /**
   * Starts that platform's scan now, or without one every platform not already scanning (202);
   * 409 if the requested scan (without a platform: both) is already going.
   */
  runScan: (platform?: Platform) => request<ScanStarted>('POST', '/api/scans/run', { query: { platform } }),
  scans: (q: Paging = {}, signal?: AbortSignal) =>
    request<ListResponse<ScanRun>>('GET', '/api/scans', { query: paging(q), signal }),

  // Outreach queue
  outreach: (q: OutreachQuery = {}, signal?: AbortSignal) =>
    request<ListResponse<OutreachMessage>>('GET', '/api/outreach', {
      query: { kind: q.kind, status: q.status, ...paging(q) },
      signal,
    }),
  updateOutreach: (id: number, body: OutreachUpdate) =>
    request<OutreachMessage>('PATCH', `/api/outreach/${id}`, { body }),
  approveOutreach: (id: number) => request<OutreachMessage>('POST', `/api/outreach/${id}/approve`),
  rejectOutreach: (id: number) => request<OutreachMessage>('POST', `/api/outreach/${id}/reject`),

  // Template Library
  /** All templates, or only those of one kind. */
  templates: (kind?: TemplateKind, signal?: AbortSignal) => fetchAll<Template>('/api/templates', { kind }, signal),
  createTemplate: (body: TemplateInput) => request<Template>('POST', '/api/templates', { body }),
  updateTemplate: (id: number, body: TemplateInput) => request<Template>('PUT', `/api/templates/${id}`, { body }),
  deleteTemplate: (id: number) => request<void>('DELETE', `/api/templates/${id}`),

  // Profile
  profile: (signal?: AbortSignal) => request<Profile>('GET', '/api/profile', { signal }),
  /** Inserts or updates fields by key; returns the full profile. */
  updateProfileFields: (body: ProfileFieldsUpdate) => request<Profile>('PUT', '/api/profile/fields', { body }),
  deleteProfileField: (key: string) => request<void>('DELETE', `/api/profile/fields/${encodeURIComponent(key)}`),

  // Unanswered questions
  unansweredQuestions: (signal?: AbortSignal) =>
    fetchAll<UnansweredQuestion>('/api/questions/unanswered', {}, signal),
  answerQuestion: (id: number, body: AnswerInput) =>
    request<Template>('POST', `/api/questions/${id}/answer`, { body }),
}

/** Human-readable message for any error thrown by the API layer. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return String(err)
}
