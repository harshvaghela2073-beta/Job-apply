import { useState } from 'react'
import { api } from '../api/client'
import { PLATFORMS, type ApplyType, type Platform } from '../api/types'
import { CollapsibleSection } from '../components/CollapsibleSection'
import { EmptyState, QueryBoundary } from '../components/DataState'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, RefreshButton } from '../components/PageHeader'
import { Pager } from '../components/Pager'
import { RemovedToast } from '../components/RemovedToast'
import { ScannerPanel } from '../components/ScannerPanel'
import { useCounts } from '../hooks/useCounts'
import { useJobRemoval } from '../hooks/useJobRemoval'
import { PAGE_SIZE, useStepBackWhenEmpty } from '../hooks/usePaging'
import { useQuery } from '../hooks/useQuery'
import { useScanStatus } from '../hooks/useScanStatus'
import { APPLY_TYPE_LABEL, PLATFORM_LABEL } from '../lib/format'
import { APPLY_TYPES_BY_PLATFORM, LISTED_STATUSES } from '../lib/jobs'

/** Every apply type once, LinkedIn's first. */
const ALL_TYPES = [...new Set(PLATFORMS.flatMap((p) => APPLY_TYPES_BY_PLATFORM[p]))]

export function DashboardPage() {
  const { refresh } = useCounts()
  const [platform, setPlatform] = useState<Platform | ''>('')
  const [applyType, setApplyType] = useState<ApplyType | ''>('')
  const [offset, setOffset] = useState(0)

  // Every listed job from both platforms, newest found first.
  const jobs = useQuery(`all-jobs:${platform}:${applyType}:${offset}`, (signal) =>
    api.jobs(
      {
        platform: platform || undefined,
        apply_type: applyType || undefined,
        status: LISTED_STATUSES,
        sort: 'found',
        limit: PAGE_SIZE,
        offset,
      },
      signal,
    ),
  )
  useStepBackWhenEmpty(jobs.data?.items.length, offset, setOffset)
  const removal = useJobRemoval(jobs.data?.items, () => {
    jobs.reload()
    refresh()
  })

  // When a scanner finishes there may be new jobs.
  const scan = useScanStatus(() => {
    jobs.reload()
    refresh()
  })

  const types = platform ? APPLY_TYPES_BY_PLATFORM[platform] : ALL_TYPES
  const choosePlatform = (p: Platform | '') => {
    setPlatform(p)
    if (p && applyType && !APPLY_TYPES_BY_PLATFORM[p].includes(applyType)) setApplyType('')
    setOffset(0)
  }
  const chooseType = (t: ApplyType | '') => {
    setApplyType(t)
    setOffset(0)
  }

  const reloadAll = () => {
    refresh()
    jobs.reload()
    scan.query.reload()
  }

  return (
    <main className="page">
      <PageHeader
        title="Dashboard"
        actions={<RefreshButton onClick={reloadAll} loading={jobs.loading || scan.query.loading} />}
      />

      <ScannerPanel scan={scan} />

      <CollapsibleSection
        id="dashboard.jobs"
        title="All jobs"
        meta={jobs.data ? <span className="num">{removal.view(jobs.data).total.toLocaleString()}</span> : undefined}
      >
        <div className="filter-bar">
          <div className="segmented" role="group" aria-label="Platform">
            {(['', ...PLATFORMS] as const).map((p) => (
              <button key={p || 'all'} type="button" aria-pressed={platform === p} onClick={() => choosePlatform(p)}>
                {p ? PLATFORM_LABEL[p] : 'All'}
              </button>
            ))}
          </div>
          <div className="segmented" role="group" aria-label="Apply type">
            {(['', ...types] as const).map((t) => (
              <button key={t || 'all'} type="button" aria-pressed={applyType === t} onClick={() => chooseType(t)}>
                {t ? APPLY_TYPE_LABEL[t] : 'All types'}
              </button>
            ))}
          </div>
        </div>

        <QueryBoundary
          query={jobs}
          loadingLabel="Loading jobs…"
          isEmpty={(d) => removal.view(d).items.length === 0}
          empty={<EmptyState title={platform || applyType ? 'No matching jobs.' : 'No jobs yet.'} />}
        >
          {(d) => {
            const v = removal.view(d)
            return (
              <>
                <JobsTable
                  jobs={v.items}
                  columns={['job', 'platform', 'type', 'location', 'posted', 'found', 'status']}
                  removal={removal}
                />
                <Pager total={v.total} offset={offset} limit={PAGE_SIZE} shown={v.items.length} onChange={setOffset} />
              </>
            )
          }}
        </QueryBoundary>
      </CollapsibleSection>
      <RemovedToast removal={removal} />
    </main>
  )
}
