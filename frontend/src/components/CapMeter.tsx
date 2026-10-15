import type { CapUsage } from '../api/types'

/** Used/limit bar. The fill turns amber at 80% and red when the cap is reached. */
export function CapMeter({ label, cap, period }: { label: string; cap: CapUsage; period: string }) {
  const { used, limit } = cap
  const ratio = limit > 0 ? Math.min(used / limit, 1) : used > 0 ? 1 : 0
  const level = ratio >= 1 ? 'critical' : ratio >= 0.8 ? 'warning' : 'ok'
  const remaining = Math.max(limit - used, 0)
  return (
    <div className={`meter meter-${level}`}>
      <div className="meter-head">
        <span className="meter-label">{label}</span>
        <span className="meter-value">
          <strong>{used}</strong> / {limit}
        </span>
      </div>
      <div
        className="meter-track"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={used}
        aria-valuetext={`${used} of ${limit} used ${period}`}
      >
        <div className="meter-fill" style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="meter-foot muted">
        {level === 'critical' ? `Cap reached ${period}` : `${remaining} left ${period}`}
      </div>
    </div>
  )
}
