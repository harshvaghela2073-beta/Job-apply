import type { ApplyType, Job, JobStatus, Platform } from '../api/types'

/** Columns JobsTable can show. `status` (Applied / Pending) always goes last. */
export type JobColumn =
  'job' | 'platform' | 'type' | 'location' | 'experience' | 'email' | 'posted' | 'found' | 'open' | 'status'

/** Columns that older rows or some sources leave empty; dropped when no job in the list has a value. */
const OPTIONAL_VALUES: Partial<Record<JobColumn, (job: Job) => string | null>> = {
  experience: (job) => job.experience,
  location: (job) => job.location,
}

/** Drops the optional columns that no job in the list has a value for. */
export function columnsWithData(columns: JobColumn[], jobs: Job[]): JobColumn[] {
  return columns.filter((c) => {
    const get = OPTIONAL_VALUES[c]
    return !get || jobs.some((job) => !!get(job))
  })
}

/** Statuses the job lists request: removed (`skipped`) and failed jobs stay out of them. */
export const LISTED_STATUSES: JobStatus[] = ['found', 'needs_manual', 'applied']

/** `status_reason` of a job the candidate removed with the × button. */
export const REMOVED_REASON = 'Removed by you.'

/** The UI shows two states: Applied (`applied`) and Pending (every other status). */
export const isApplied = (job: Pick<Job, 'status'>) => job.status === 'applied'

/** Link to the job itself (the posting, or the hiring post). */
export const jobLink = (job: Job) => job.url ?? job.post_url

/**
 * Where to apply by hand: the Google Form, or the company's application page (LinkedIn's search
 * often has none, so the job page, whose Apply button leads there). null for other apply types.
 */
export function applyLink(job: Job): { href: string; label: string } | null {
  if (job.apply_type === 'google_form') return job.form_url ? { href: job.form_url, label: 'Open form' } : null
  if (job.apply_type === 'company_site') {
    const href = job.apply_url ?? job.url
    return href ? { href, label: 'Open' } : null
  }
  return null
}

/** Apply types each platform produces, in the order the filters show them. */
export const APPLY_TYPES_BY_PLATFORM: Record<Platform, ApplyType[]> = {
  linkedin: ['easy_apply', 'company_site', 'google_form', 'email'],
  naukri: ['one_click', 'company_site'],
}
