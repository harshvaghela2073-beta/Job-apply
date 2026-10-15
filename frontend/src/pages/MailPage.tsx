import { useState } from 'react'
import { api } from '../api/client'
import { OUTREACH_KINDS, type OutreachKind } from '../api/types'
import { CollapsibleSection } from '../components/CollapsibleSection'
import { EmptyState, QueryBoundary } from '../components/DataState'
import { OutreachCard } from '../components/OutreachCard'
import { Pager } from '../components/Pager'
import { PAGE_SIZE, useStepBackWhenEmpty } from '../hooks/usePaging'
import { useQuery } from '../hooks/useQuery'
import { OUTREACH_KIND_LABEL } from '../lib/format'
import { JobListPage } from './JobListPage'

/** The outreach queue (emails and connection notes the tool sent or prepared), newest first. */
function SentMessages() {
  const [kind, setKind] = useState<OutreachKind | ''>('')
  const [offset, setOffset] = useState(0)
  const messages = useQuery(`outreach:${kind}:${offset}`, (signal) =>
    api.outreach({ kind: kind || undefined, limit: PAGE_SIZE, offset }, signal),
  )
  useStepBackWhenEmpty(messages.data?.items.length, offset, setOffset)

  return (
    <CollapsibleSection
      id="linkedin.mail.sent"
      title="Sent messages"
      defaultOpen={false}
      className="section-spaced"
      meta={messages.data ? <span className="num">{messages.data.total.toLocaleString()}</span> : undefined}
    >
      <div className="filter-bar">
        <div className="segmented" role="group" aria-label="Message kind">
          {(['', ...OUTREACH_KINDS] as const).map((k) => (
            <button
              key={k || 'all'}
              type="button"
              aria-pressed={kind === k}
              onClick={() => {
                setKind(k)
                setOffset(0)
              }}
            >
              {k ? OUTREACH_KIND_LABEL[k] : 'All'}
            </button>
          ))}
        </div>
      </div>
      <QueryBoundary
        query={messages}
        loadingLabel="Loading messages…"
        isEmpty={(d) => d.items.length === 0}
        empty={<EmptyState title="No messages yet." />}
      >
        {(d) => (
          <>
            <div className="card-list">
              {d.items.map((msg) => (
                <OutreachCard key={`${msg.id}:${msg.status}`} msg={msg} compact onChanged={messages.reload} />
              ))}
            </div>
            <Pager total={d.total} offset={offset} limit={PAGE_SIZE} shown={d.items.length} onChange={setOffset} />
          </>
        )}
      </QueryBoundary>
    </CollapsibleSection>
  )
}

/** LinkedIn hiring posts that ask for an email (Applied = emailed), then the sent messages. */
export function MailPage() {
  return (
    <JobListPage title="Mail" platform="linkedin" applyType="email" columns={['job', 'email', 'posted', 'status']}>
      <SentMessages />
    </JobListPage>
  )
}
