import { useId, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { PLATFORMS, type Analytics, type AnalyticsPeriod, type ApplyCounts, type DashboardSummary } from '../api/types'
import { CapMeter } from '../components/CapMeter'
import { QueryBoundary } from '../components/DataState'
import { PageHeader, RefreshButton } from '../components/PageHeader'
import { useCounts } from '../hooks/useCounts'
import { useQuery } from '../hooks/useQuery'
import { PLATFORM_LABEL } from '../lib/format'

type Period = keyof Analytics

const PERIODS: { value: Period; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: 'all', label: 'All time' },
]

interface Part {
  label: string
  counts: ApplyCounts
  /** What `applied` means here ("Sent" for emails). */
  appliedAs?: string
}

const fmt = (n: number) => n.toLocaleString()

function total(parts: ApplyCounts[]): ApplyCounts {
  return parts.reduce(
    (sum, c) => ({ found: sum.found + c.found, applied: sum.applied + c.applied, pending: sum.pending + c.pending }),
    { found: 0, applied: 0, pending: 0 },
  )
}

const RADIUS = 46
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

/** A ring: the Applied share in one colour, Pending in the other; Applied in the middle. */
function Donut({ label, counts, appliedAs = 'Applied', size = 'sm' }: Part & { size?: 'sm' | 'lg' }) {
  const { found, applied, pending } = counts
  const share = found > 0 ? applied / found : 0
  const percent = Math.round(share * 100)
  return (
    <figure className={`donut donut-${size}`}>
      <svg
        viewBox="0 0 120 120"
        role="img"
        aria-label={`${label}: ${fmt(applied)} ${appliedAs.toLowerCase()}, ${fmt(pending)} pending of ${fmt(found)}`}
      >
        <circle className={found > 0 ? 'donut-pending' : 'donut-empty'} cx="60" cy="60" r={RADIUS} />
        {applied > 0 && (
          <circle
            className="donut-applied"
            cx="60"
            cy="60"
            r={RADIUS}
            strokeDasharray={`${share * CIRCUMFERENCE} ${CIRCUMFERENCE}`}
            transform="rotate(-90 60 60)"
          />
        )}
        <text x="60" y="58" className="donut-value">
          {fmt(applied)}
        </text>
        <text x="60" y="76" className="donut-sub">
          {found > 0 ? `${percent}% of ${fmt(found)}` : 'none yet'}
        </text>
      </svg>
      <figcaption>
        <span className="donut-label">{label}</span>
        <span className="donut-legend">
          <span className="donut-key donut-key-applied">
            {appliedAs} {fmt(applied)}
          </span>
          <span className="donut-key donut-key-pending">Pending {fmt(pending)}</span>
        </span>
      </figcaption>
    </figure>
  )
}

/** One platform: its total ring, then a ring per apply type. */
function PlatformCard({ title, parts, footer }: { title: string; parts: Part[]; footer?: ReactNode }) {
  const headingId = useId()
  return (
    <section className="panel analysis-card" aria-labelledby={headingId}>
      <h2 className="analysis-card-title" id={headingId}>
        {title}
      </h2>
      <div className="analysis-rings">
        <Donut label="All" counts={total(parts.map((p) => p.counts))} size="lg" />
        <div className="analysis-ring-grid">
          {parts.map((part) => (
            <Donut key={part.label} {...part} />
          ))}
        </div>
      </div>
      {footer}
    </section>
  )
}

function linkedinParts(data: AnalyticsPeriod): Part[] {
  const { linkedin } = data
  return [
    { label: 'Easy Apply', counts: linkedin.easy_apply },
    { label: 'Company website', counts: linkedin.company_site },
    { label: 'Google Form', counts: linkedin.google_form },
    { label: 'Email', counts: linkedin.email, appliedAs: 'Sent' },
  ]
}

function naukriParts(data: AnalyticsPeriod): Part[] {
  return [
    { label: 'One-click', counts: data.naukri.one_click },
    { label: 'Company website', counts: data.naukri.company_site },
  ]
}

/** Every application so far, today and all time. */
function Overall({ analytics }: { analytics: Analytics }) {
  const overall = (period: Period) =>
    total([...linkedinParts(analytics[period]), ...naukriParts(analytics[period])].map((p) => p.counts))
  return (
    <section className="panel analysis-card analysis-overall" aria-label="All applications">
      <h2 className="analysis-card-title">All applications</h2>
      <div className="analysis-overall-rings">
        <Donut label="Found today" counts={overall('today')} size="lg" />
        <Donut label="All time" counts={overall('all')} size="lg" />
      </div>
    </section>
  )
}

function PlatformCards({ data }: { data: AnalyticsPeriod }) {
  return (
    <div className="analysis-grid">
      <PlatformCard
        title="LinkedIn"
        parts={linkedinParts(data)}
        footer={
          <div className="analysis-extra">
            <span>Connection notes sent</span>
            <strong className="num">{fmt(data.linkedin.connection_notes_sent)}</strong>
          </div>
        }
      />
      <PlatformCard title="Naukri" parts={naukriParts(data)} />
    </div>
  )
}

function StatCard({ label, value, sub, to }: { label: string; value: number; sub?: ReactNode; to?: string }) {
  const body = (
    <>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{fmt(value)}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </>
  )
  return to ? (
    <Link to={to} className="stat stat-link">
      {body}
    </Link>
  ) : (
    <div className="stat">{body}</div>
  )
}

/** Applied counts, unanswered questions and the daily / monthly caps (GET /api/dashboard/summary). */
function Summary({ s }: { s: DashboardSummary }) {
  return (
    <>
      <div className="stats">
        <StatCard label="Applied today" value={s.applied_today} />
        <StatCard
          label="Applied total"
          value={s.applied_total}
          sub={PLATFORMS.map((p) => `${PLATFORM_LABEL[p]} ${fmt(s.applied_by_platform[p] ?? 0)}`).join(' · ')}
        />
        <StatCard label="Unanswered questions" value={s.unanswered_questions} to="/templates" />
      </div>
      <h2 className="section-title section-title-spaced">Caps</h2>
      <div className="meters">
        <CapMeter label="LinkedIn Easy Apply" cap={s.caps.easy_apply_today} period="today" />
        <CapMeter label="Naukri apply" cap={s.caps.naukri_apply_today} period="today" />
        <CapMeter label="Connection notes" cap={s.caps.connection_notes_month} period="this month" />
        <CapMeter label="InMail" cap={s.caps.inmail_month} period="this month" />
      </div>
    </>
  )
}

export function AnalysisPage() {
  const [period, setPeriod] = useState<Period>('today')
  const analytics = useQuery('analytics', (signal) => api.analytics(signal))
  const { summary, refresh } = useCounts()

  return (
    <main className="page">
      <PageHeader
        title="Analysis"
        actions={
          <>
            <div className="segmented" role="group" aria-label="Period">
              {PERIODS.map((p) => (
                <button
                  key={p.value}
                  type="button"
                  aria-pressed={period === p.value}
                  onClick={() => setPeriod(p.value)}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <RefreshButton
              onClick={() => {
                analytics.reload()
                refresh()
              }}
              loading={analytics.loading || summary.loading}
            />
          </>
        }
      />

      <QueryBoundary query={analytics} loadingLabel="Loading analysis…">
        {(a) => (
          <>
            <Overall analytics={a} />
            <PlatformCards data={a[period]} />
          </>
        )}
      </QueryBoundary>

      <h2 className="section-title section-title-spaced">Summary</h2>
      <QueryBoundary query={summary} loadingLabel="Loading summary…">
        {(s) => <Summary s={s} />}
      </QueryBoundary>
    </main>
  )
}
