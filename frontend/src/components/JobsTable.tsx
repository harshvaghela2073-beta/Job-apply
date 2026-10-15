import { useState } from 'react'
import { api, errorMessage } from '../api/client'
import type { Job, JobStatusUpdateValue } from '../api/types'
import { useNow } from '../hooks/usePolling'
import { APPLY_TYPE_LABEL, formatDateTime, formatRelative, formatUtc, JOB_STATUS_LABEL } from '../lib/format'
import { applyLink, isApplied, jobLink, type JobColumn as Column } from '../lib/jobs'
import { PlatformTag } from './Badge'
import { ExternalLink } from './ExternalLink'

const HEADERS: Record<Column, string> = {
  job: 'Job',
  platform: 'Platform',
  type: 'Type',
  location: 'Location',
  experience: 'Experience',
  email: 'Email',
  posted: 'Posted',
  found: 'Found',
  open: 'Apply',
  status: 'Status',
}

const dash = <span className="muted">—</span>

/** "3 h ago", with the full local and UTC time on hover. */
function RelativeTime({ value, now }: { value: string | null; now: number }) {
  if (!value) return dash
  return (
    <time dateTime={value} title={`${formatDateTime(value)} (${formatUtc(value)})`} className="nowrap">
      {formatRelative(value, now)}
    </time>
  )
}

/** Hover text of the status toggle: when it was applied, or why it is still pending. */
function statusHint(job: Job): string | undefined {
  if (isApplied(job)) return job.applied_at ? `Applied ${formatDateTime(job.applied_at)}` : undefined
  const parts = [job.status !== 'found' ? JOB_STATUS_LABEL[job.status] : null, job.status_reason]
  return parts.filter(Boolean).join(' — ') || undefined
}

interface StatusToggleProps {
  job: Job
  saving: boolean
  error: string | undefined
  onToggle: () => void
}

/** Applied / Pending pill; a click switches between the two. */
function StatusToggle({ job, saving, error, onToggle }: StatusToggleProps) {
  const applied = isApplied(job)
  return (
    <>
      <button
        type="button"
        className={`status-toggle${applied ? ' is-applied' : ''}`}
        aria-pressed={applied}
        aria-label={`Applied: ${job.title}, ${job.company}`}
        aria-disabled={saving || undefined}
        aria-busy={saving || undefined}
        title={statusHint(job)}
        onClick={() => {
          if (!saving) onToggle()
        }}
      >
        <span className="status-toggle-mark" aria-hidden="true" />
        {applied ? 'Applied' : 'Pending'}
      </button>
      {error && (
        <span className="status-error" role="alert" title={error}>
          Not saved<span className="sr-only">: {error}</span>
        </span>
      )}
    </>
  )
}

function Cell({ job, column, now }: { job: Job; column: Exclude<Column, 'status'>; now: number }) {
  switch (column) {
    case 'job': {
      const link = jobLink(job)
      return (
        <>
          {link ? (
            <ExternalLink href={link} className="title-link">
              {job.title}
            </ExternalLink>
          ) : (
            <span className="title-text">{job.title}</span>
          )}
          <div className="job-company">{job.company}</div>
        </>
      )
    }
    case 'platform':
      return <PlatformTag platform={job.platform} />
    case 'type':
      return <span className="nowrap">{APPLY_TYPE_LABEL[job.apply_type] ?? job.apply_type}</span>
    case 'location':
      return job.location ? <>{job.location}</> : dash
    case 'experience':
      return job.experience ? <span className="nowrap">{job.experience}</span> : dash
    case 'email':
      return job.contact_email ? (
        <a href={`mailto:${job.contact_email}`} className="break-all">
          {job.contact_email}
        </a>
      ) : (
        dash
      )
    case 'posted':
      return <RelativeTime value={job.posted_at} now={now} />
    case 'found':
      return <RelativeTime value={job.found_at} now={now} />
    case 'open': {
      const target = applyLink(job)
      return target ? (
        <ExternalLink href={target.href} className="btn btn-sm">
          {target.label} <span aria-hidden="true">↗</span>
        </ExternalLink>
      ) : (
        dash
      )
    }
  }
}

/** What the job will look like once the backend saves `status` (shown while the request runs). */
function withStatus(job: Job, status: JobStatusUpdateValue): Job {
  const applied = status === 'applied'
  return {
    ...job,
    status,
    status_reason: null,
    applied_at: applied ? (job.applied_at ?? new Date().toISOString()) : null,
  }
}

/** A status change made in this table, shown until the list is reloaded with the saved job. */
interface Override {
  /** `updated_at` of the listed job it applies to; a reloaded (changed) job drops it. */
  base: string
  job: Job
  saving: boolean
}

interface JobsTableProps {
  jobs: Job[]
  /** Status is always shown last, whether or not it is listed here. */
  columns: Column[]
  /** Called with the saved job after its status changes. */
  onStatusChange?: (job: Job) => void
}

/** Shared job list: Dashboard, LinkedIn and Naukri tabs. The Status column toggles Applied / Pending. */
export function JobsTable({ jobs, columns, onStatusChange }: JobsTableProps) {
  const now = useNow()
  const [overrides, setOverrides] = useState<Record<number, Override>>({})
  const [errors, setErrors] = useState<Record<number, string>>({})
  const shown = columns.filter((c): c is Exclude<Column, 'status'> => c !== 'status')

  const current = (job: Job): Override | undefined => {
    const o = overrides[job.id]
    return o && o.base === job.updated_at ? o : undefined
  }

  const toggle = async (listed: Job) => {
    const before = current(listed)
    const view = before?.job ?? listed
    const next: JobStatusUpdateValue = isApplied(view) ? 'found' : 'applied'
    const optimistic = withStatus(view, next)
    setOverrides((o) => ({ ...o, [listed.id]: { base: listed.updated_at, job: optimistic, saving: true } }))
    setErrors((e) => {
      const rest = { ...e }
      delete rest[listed.id]
      return rest
    })
    try {
      const saved = await api.updateJob(listed.id, { status: next })
      setOverrides((o) => ({ ...o, [listed.id]: { base: listed.updated_at, job: saved, saving: false } }))
      onStatusChange?.(saved)
    } catch (err) {
      // Roll back to what was shown before the click.
      setOverrides((o) => {
        const rest = { ...o }
        if (before) rest[listed.id] = before
        else delete rest[listed.id]
        return rest
      })
      setErrors((e) => ({ ...e, [listed.id]: errorMessage(err) }))
    }
  }

  return (
    <div className="table-wrap">
      <table className="table jobs-table">
        <thead>
          <tr>
            {shown.map((c) => (
              <th key={c} className={`col-${c}`} scope="col">
                {HEADERS[c]}
              </th>
            ))}
            <th className="col-status" scope="col">
              {HEADERS.status}
            </th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((listed) => {
            const override = current(listed)
            const job = override?.job ?? listed
            return (
              <tr key={job.id} className={isApplied(job) ? 'row-applied' : undefined}>
                {shown.map((c) => (
                  <td key={c} className={`col-${c}`} data-label={HEADERS[c]}>
                    <Cell job={job} column={c} now={now} />
                  </td>
                ))}
                <td className="col-status">
                  <StatusToggle
                    job={job}
                    saving={override?.saving ?? false}
                    error={errors[job.id]}
                    onToggle={() => void toggle(listed)}
                  />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
