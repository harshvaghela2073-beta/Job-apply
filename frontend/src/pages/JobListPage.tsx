import { useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type { ApplyType, Platform } from '../api/types'
import { EmptyState, QueryBoundary } from '../components/DataState'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, RefreshButton } from '../components/PageHeader'
import { Pager } from '../components/Pager'
import { RemovedToast } from '../components/RemovedToast'
import { useCounts } from '../hooks/useCounts'
import { useJobRemoval } from '../hooks/useJobRemoval'
import { PAGE_SIZE, useStepBackWhenEmpty } from '../hooks/usePaging'
import { useQuery } from '../hooks/useQuery'
import { columnsWithData, LISTED_STATUSES, type JobColumn } from '../lib/jobs'

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
  const { refresh } = useCounts()
  const [offset, setOffset] = useState(0)
  const jobs = useQuery(`jobs:${platform ?? 'all'}:${applyType}:${offset}`, (signal) =>
    api.jobs(
      { platform, apply_type: applyType, status: LISTED_STATUSES, sort: 'posted', limit: PAGE_SIZE, offset },
      signal,
    ),
  )
  useStepBackWhenEmpty(jobs.data?.items.length, offset, setOffset)
  const removal = useJobRemoval(jobs.data?.items, () => {
    jobs.reload()
    refresh()
  })

  return (
    <main className="page">
      <PageHeader title={title} actions={<RefreshButton onClick={jobs.reload} loading={jobs.loading} />} />
      <QueryBoundary
        query={jobs}
        loadingLabel="Loading jobs…"
        isEmpty={(d) => removal.view(d).items.length === 0}
        empty={<EmptyState title="No jobs yet." />}
      >
        {(d) => {
          const v = removal.view(d)
          return (
            <>
              <JobsTable jobs={v.items} columns={columnsWithData(columns, v.items)} removal={removal} />
              <Pager total={v.total} offset={offset} limit={PAGE_SIZE} shown={v.items.length} onChange={setOffset} />
            </>
          )
        }}
      </QueryBoundary>
      {children}
      <RemovedToast removal={removal} />
    </main>
  )
}
