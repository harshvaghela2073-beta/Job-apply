import type { ReactNode } from 'react'
import type { OutreachStatus, Platform } from '../api/types'
import { OUTREACH_STATUS_LABEL, OUTREACH_STATUS_TONE, PLATFORM_LABEL, type Tone } from '../lib/format'

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  )
}

export function OutreachStatusBadge({ status }: { status: OutreachStatus }) {
  return <Badge tone={OUTREACH_STATUS_TONE[status]}>{OUTREACH_STATUS_LABEL[status]}</Badge>
}

export function PlatformTag({ platform }: { platform: Platform }) {
  return <span className={`platform platform-${platform}`}>{PLATFORM_LABEL[platform]}</span>
}

/** Small count bubble for tabs. Hidden when the count is 0 or unknown. */
export function CountBubble({ count, label }: { count: number | undefined; label?: string }) {
  if (!count) return null
  return (
    <span className="count-bubble" aria-label={label ? `${count} ${label}` : String(count)}>
      {count > 99 ? '99+' : count}
    </span>
  )
}
