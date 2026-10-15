import { useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type { ApplyType, Platform } from '../api/types'
import { EmptyState, QueryBoundary } from '../components/DataState'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, RefreshButton } from '../components/PageHeader'
import { Pager } from '../components/Pager'
import { PAGE_SIZE, useStepBackWhenEmpty } from '../hooks/usePaging'
import { useQuery } from '../hooks/useQuery'
import { columnsWithData, type JobColumn } from '../lib/jobs'

interface JobListPageProps {
  title: string
  /** Omitted for Google Forms, which only come from LinkedIn posts anyway. */
  platform?: Platform
  applyType: ApplyType
  /** Location and experience are dropped when no job on the page has them; Status always goes last. */
  columns: JobColumn[]
  /** Shown under the list (the Mail tab's sent messages). */
  children?: ReactNode
}

/** One LinkedIn or Naukri sub-tab: the jobs of one apply type, newest posted first. */
export function JobListPage({ title, platform, applyType, columns, children }: JobListPageProps) {
  const [offset, setOffset] = useState(0)
  const jobs = useQuery(`jobs:${platform ?? 'all'}:${applyType}:${offset}`, (signal) =>
    api.jobs({ platform, apply_type: applyType, sort: 'posted', limit: PAGE_SIZE, offset }, signal),
  )
  useStepBackWhenEmpty(jobs.data?.items.length, offset, setOffset)

  return (
    <main className="page">
      <PageHeader title={title} actions={<RefreshButton onClick={jobs.reload} loading={jobs.loading} />} />
      <QueryBoundary
        query={jobs}
        loadingLabel="Loading jobs…"
        isEmpty={(d) => d.items.length === 0}
        empty={<EmptyState title="No jobs yet." />}
      >
        {(d) => (
          <>
            <JobsTable jobs={d.items} columns={columnsWithData(columns, d.items)} />
            <Pager total={d.total} offset={offset} limit={PAGE_SIZE} shown={d.items.length} onChange={setOffset} />
          </>
        )}
      </QueryBoundary>
      {children}
    </main>
  )
}
