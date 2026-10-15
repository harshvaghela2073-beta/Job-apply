import { useId, useState } from 'react'
import { api, errorMessage } from '../api/client'
import {
  INTERVAL_RANGE,
  PLATFORMS,
  SCAN_INTERVAL_SETTING,
  SCAN_KINDS_BY_PLATFORM,
  type AppSettings,
  type IntervalKey,
  type Platform,
  type ScanKind,
  type ScanRun,
  type ScanStatus,
  type SettingKey,
} from '../api/types'
import { shownStatus, useConnections } from '../hooks/useConnections'
import { useQuery } from '../hooks/useQuery'
import type { RunState, ScanStatusValue } from '../hooks/useScanStatus'
import {
  CONNECTION_STATUS_LABEL,
  formatClock,
  formatDateTime,
  parseApiDate,
  PLATFORM_LABEL,
  SCAN_STATUS_LABEL,
  SCAN_STATUS_TONE,
} from '../lib/format'
import { Badge } from './Badge'
import { CollapsibleSection } from './CollapsibleSection'
import { ConnectButton, StatusDot } from './Connections'
import { ErrorState, InlineError } from './DataState'

/** One feature switch: the scanner it controls and the setting behind it. */
interface Feature {
  kind: ScanKind
  label: string
  setting: SettingKey
  /** The setting pauses the feature, so the switch (on = running) shows its inverse. */
  inverted?: boolean
}

interface PlatformGroup {
  platform: Platform
  /** Pauses the whole platform; the switch shows its inverse. */
  pause: SettingKey
  /** How often the platform's searches run inside its scans (LinkedIn only). */
  searchInterval?: IntervalKey
  features: Feature[]
}

/** LinkedIn, then Naukri; each scan runs its features in this order. */
const GROUPS: PlatformGroup[] = [
  {
    platform: 'linkedin',
    pause: 'pause_linkedin',
    searchInterval: 'linkedin_search_interval_minutes',
    features: [
      { kind: 'linkedin_jobs', label: 'Job search', setting: 'pause_linkedin_jobs', inverted: true },
      { kind: 'linkedin_apply', label: 'Easy Apply', setting: 'auto_apply_linkedin' },
      {
        kind: 'linkedin_posts',
        label: 'Hiring posts – Google Form & email',
        setting: 'pause_linkedin_posts',
        inverted: true,
      },
      { kind: 'email_send', label: 'Send emails', setting: 'auto_send_email' },
      { kind: 'linkedin_outreach', label: 'Connection notes', setting: 'auto_send_linkedin_notes' },
    ],
  },
  {
    platform: 'naukri',
    pause: 'pause_naukri',
    features: [
      { kind: 'naukri_jobs', label: 'Job search', setting: 'pause_naukri_jobs', inverted: true },
      { kind: 'naukri_apply', label: 'One-click apply', setting: 'auto_apply_naukri' },
    ],
  },
]

/** The value a switch shows (on = running); undefined when the backend doesn't know the setting. */
function switchValue(settings: AppSettings | undefined, key: SettingKey, inverted: boolean): boolean | undefined {
  const value = settings?.[key]
  return value === undefined ? undefined : inverted ? !value : value
}

function withSetting(settings: AppSettings, key: SettingKey, value: boolean): AppSettings {
  const next = { ...settings }
  next[key] = value
  return next
}

/**
 * GET /api/settings with optimistic updates: a switch flips at once and goes back if the save
 * fails. Each setting is saved on its own, so flipping two switches quickly doesn't mix them up.
 */
function useSettingSwitches() {
  const query = useQuery('settings', (signal) => api.settings(signal))
  const [values, setValues] = useState<AppSettings | undefined>(query.data)
  const [seen, setSeen] = useState(query.data)
  if (query.data !== seen) {
    setSeen(query.data)
    setValues(query.data)
  }
  const [saving, setSaving] = useState<Partial<Record<SettingKey, boolean>>>({})
  const [error, setError] = useState<string | null>(null)

  const save = async (key: SettingKey, value: boolean, label: string) => {
    const previous = values?.[key]
    if (previous === undefined) return
    setValues((v) => v && withSetting(v, key, value))
    setSaving((s) => ({ ...s, [key]: true }))
    setError(null)
    try {
      const body: Partial<AppSettings> = {}
      body[key] = value
      const saved = await api.updateSettings(body)
      setValues((v) => v && withSetting(v, key, saved[key] ?? value))
    } catch (err) {
      setValues((v) => v && withSetting(v, key, previous))
      setError(`${label}: ${errorMessage(err)}`)
    } finally {
      setSaving((s) => ({ ...s, [key]: false }))
    }
  }

  return { query, values, saving, error, clearError: () => setError(null), save }
}

interface SwitchProps {
  label: string
  /** undefined: the backend doesn't have this setting (shown off and disabled). */
  on: boolean | undefined
  disabled: boolean
  saving: boolean
  onChange: (on: boolean) => void
  className?: string
  /** Hide the label visually (it is still the switch's accessible name). */
  hideLabel?: boolean
}

function Switch({ label, on, disabled, saving, onChange, className, hideLabel = false }: SwitchProps) {
  return (
    <label className={`switch${className ? ` ${className}` : ''}`}>
      <input
        type="checkbox"
        role="switch"
        checked={on ?? false}
        disabled={disabled || on === undefined}
        aria-busy={saving || undefined}
        onChange={(e) => {
          // Ignored while this switch's last change is still being saved.
          if (!saving) onChange(e.target.checked)
        }}
      />
      <span className={hideLabel ? 'sr-only' : 'switch-label'}>{label}</span>
    </label>
  )
}

/** "OK · 14:03 · 12 found · 3 new"; the run's message shows on hover. */
function RunStatus({ run }: { run: ScanRun | undefined }) {
  if (!run) return <span className="run-status muted">Not run yet</span>
  const when = run.finished_at ?? run.started_at
  return (
    <span className="run-status" title={run.message ?? undefined}>
      <span className="run-badge">
        {run.status === 'running' && <span className="spinner spinner-sm" aria-hidden="true" />}
        <Badge tone={SCAN_STATUS_TONE[run.status]}>{SCAN_STATUS_LABEL[run.status]}</Badge>
      </span>
      <time dateTime={when} title={formatDateTime(when)} className="num">
        {formatClock(when)}
      </time>
      <span className="run-counts num">
        {run.found} found · {run.new} new
      </span>
      {run.message && <span className="sr-only">{run.message}</span>}
    </span>
  )
}

/** Login state of one platform, with Connect when it is missing. */
function Login({ platform }: { platform: Platform }) {
  const { byPlatform, busy, offline } = useConnections()
  if (offline) return null
  const conn = byPlatform[platform]
  const status = shownStatus(conn, busy[platform])
  return (
    <span className="scanner-login" title={conn?.message ?? undefined}>
      <StatusDot state={status ?? 'checking'} />
      <span className="scanner-login-text">
        {status === 'connecting'
          ? 'Waiting for login…'
          : status === 'connected' && conn?.account_name
            ? conn.account_name
            : status
              ? CONNECTION_STATUS_LABEL[status]
              : 'Checking…'}
      </span>
      {(status === 'disconnected' || status === 'error') && (
        <ConnectButton platform={platform} label="Connect" className="btn btn-sm" />
      )}
    </span>
  )
}

type Settings = ReturnType<typeof useSettingSwitches>

/** The latest run of each of the platform's scanners. */
function platformRuns(status: ScanStatus, platform: Platform): ScanRun[] {
  const kinds = SCAN_KINDS_BY_PLATFORM[platform]
  return status.latest.filter((run): run is ScanRun => !!run && kinds.includes(run.kind))
}

function latestTime(runs: (ScanRun | null)[], field: 'started_at' | 'finished_at'): string | null {
  let best: string | null = null
  for (const run of runs) {
    const value = run?.[field]
    if (value && (!best || parseApiDate(value) > parseApiDate(best))) best = value
  }
  return best
}

/**
 * When the platform's current scan started: its scanners that have started did so after the
 * previous scan finished, and the first of them started the scan.
 */
function scanStart(runs: ScanRun[], lastFinished: string | null): string | null {
  if (!lastFinished) return latestTime(runs, 'started_at')
  const after = parseApiDate(lastFinished)
  let first: string | null = null
  for (const run of runs) {
    const started = parseApiDate(run.started_at)
    if (started >= after && (!first || started < parseApiDate(first))) first = run.started_at
  }
  return first ?? latestTime(runs, 'started_at')
}

/** Older backends (one scan of both platforms): "Every 30 min · last 14:03 · next 14:33". */
function Schedule({ status }: { status: ScanStatus }) {
  const parts: string[] = [status.enabled ? `Every ${status.interval_minutes} min` : 'Automatic scans off']
  if (status.running) {
    const started = latestTime(status.latest, 'started_at')
    parts.push(started ? `running since ${formatClock(started)}` : 'running')
  } else {
    const last = latestTime(status.latest, 'finished_at') ?? latestTime(status.latest, 'started_at')
    if (last) parts.push(`last ${formatClock(last)}`)
    if (status.enabled && status.next_run_at) parts.push(`next ${formatClock(status.next_run_at)}`)
  }
  return (
    <span className="scanner-schedule">
      {status.running && <span className="spinner spinner-sm" aria-hidden="true" />}
      {parts.join(' · ')}
    </span>
  )
}

/** One platform's scan: "running since 14:03", or "last 14:03 · next 14:33". */
function PlatformSchedule({ status, platform }: { status: ScanStatus; platform: Platform }) {
  const group = status.platforms?.[platform]
  if (!group) return null
  const runs = platformRuns(status, platform)
  const parts: string[] = []
  if (group.running) {
    const since = scanStart(runs, group.last_finished_at)
    parts.push(since ? `running since ${formatClock(since)}` : 'running')
  } else {
    const last = group.last_finished_at ?? latestTime(runs, 'finished_at')
    if (last) parts.push(`last ${formatClock(last)}`)
    if (status.enabled && group.next_run_at) parts.push(`next ${formatClock(group.next_run_at)}`)
  }
  if (!parts.length) return null
  return (
    <span className="scanner-schedule">
      {group.running && <span className="spinner spinner-sm" aria-hidden="true" />}
      {parts.join(' · ')}
    </span>
  )
}

/** The closed panel's header: "LinkedIn next 14:33 · Naukri running". */
function ScanSummary({ status }: { status: ScanStatus }) {
  const items: { key: string; text: string; running?: boolean }[] = []
  if (!status.enabled) items.push({ key: 'off', text: 'Automatic scans off' })
  for (const platform of PLATFORMS) {
    const group = status.platforms?.[platform]
    if (!group) continue
    const name = PLATFORM_LABEL[platform]
    if (group.running) items.push({ key: platform, text: `${name} running`, running: true })
    else if (status.enabled && group.next_run_at)
      items.push({ key: platform, text: `${name} next ${formatClock(group.next_run_at)}` })
    else if (group.last_finished_at)
      items.push({ key: platform, text: `${name} last ${formatClock(group.last_finished_at)}` })
  }
  if (!items.length) return null
  return (
    <span className="scanner-schedule scanner-summary">
      {items.map((item, i) => (
        <span key={item.key} className="scanner-summary-item">
          {item.running && <span className="spinner spinner-sm" aria-hidden="true" />}
          {item.text}
          {/* After the item, so a wrapped line doesn't start with it. */}
          {i < items.length - 1 && (
            <span className="scanner-summary-sep" aria-hidden="true">
              ·
            </span>
          )}
        </span>
      ))}
    </span>
  )
}

/** "Run now"; "Scanning…" while the scan runs. The platform is part of its accessible name. */
function RunNowButton({ state, platform, onRun }: { state: RunState; platform?: Platform; onRun: () => void }) {
  const busy = state !== 'idle'
  const text = state === 'running' ? 'Scanning…' : busy ? 'Starting…' : 'Run now'
  return (
    <button
      type="button"
      className="btn btn-sm btn-primary scanner-run"
      onClick={onRun}
      disabled={busy}
      aria-label={platform ? `${text}, ${PLATFORM_LABEL[platform]}` : undefined}
    >
      {busy && <span className="spinner spinner-sm spinner-on-accent" aria-hidden="true" />}
      {text}
    </button>
  )
}

/** A minutes box. A changed value is saved only when confirmed: Save (or Enter); Cancel (or Esc) puts
 * the saved value back. */
function IntervalInput({
  label,
  context,
  setting,
  value,
  onSaved,
}: {
  /** Shown before the box, e.g. "Every". */
  label: string
  /** Added to the accessible names only, e.g. "LinkedIn scan". */
  context: string
  setting: IntervalKey
  value: number | undefined
  onSaved: () => void
}) {
  const id = useId()
  const [min, max] = INTERVAL_RANGE[setting]
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  // The value just saved, shown until `value` (reloaded from the backend) changes from `over`.
  const [stored, setStored] = useState<{ minutes: number; over: number | undefined } | null>(null)
  if (stored && value !== stored.over) setStored(null)
  const current = stored ? stored.minutes : value
  const shown = draft ?? (current === undefined ? '' : String(current))
  const minutes = Number(draft)
  const changed = draft !== null && draft !== String(current)
  const valid = changed && Number.isInteger(minutes) && minutes >= min && minutes <= max
  const name = `${context} ${label.toLowerCase()}`

  const cancel = () => {
    setDraft(null)
    setError(null)
  }

  const save = async () => {
    if (!changed) return
    if (!valid) {
      setError(`${min}–${max} min`)
      return
    }
    setSaving(true)
    setError(null)
    try {
      const body: Partial<AppSettings> = {}
      body[setting] = minutes
      const result = await api.updateSettings(body)
      setStored({ minutes: result[setting] ?? minutes, over: value })
      setDraft(null)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 2000)
      onSaved()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <span className="scanner-interval">
      <label htmlFor={id}>
        <span className="sr-only">{context} </span>
        {label}
      </label>
      <input
        id={id}
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        step={1}
        value={shown}
        disabled={value === undefined || saving}
        aria-invalid={error ? true : undefined}
        onChange={(e) => {
          setDraft(e.target.value)
          setError(null)
          setSaved(false)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void save()
          if (e.key === 'Escape') cancel()
        }}
      />
      <span className="muted">min</span>
      {changed && (
        <>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => void save()}
            disabled={saving}
            aria-label={`Save ${name} ${draft} min`}
          >
            {saving && <span className="spinner spinner-sm spinner-on-accent" aria-hidden="true" />}
            Save
          </button>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={cancel}
            disabled={saving}
            aria-label={`Cancel ${name}`}
          >
            Cancel
          </button>
        </>
      )}
      {saved && !changed && (
        <span className="muted" role="status">
          ✓ Saved
        </span>
      )}
      {error && (
        <span className="scanner-interval-error" role="alert">
          {error}
        </span>
      )}
    </span>
  )
}

function GroupCard({
  group,
  settings,
  scan,
  onIntervalSaved,
}: {
  group: PlatformGroup
  settings: Settings
  scan: ScanStatusValue
  onIntervalSaved: () => void
}) {
  const headingId = useId()
  const { platform } = group
  const name = PLATFORM_LABEL[platform]
  const { values, saving, save } = settings
  const running = switchValue(values, group.pause, true)
  const paused = running === false
  const runs = scan.query.data
  const intervals = settings.query.data

  return (
    <section className={`scanner-group${paused ? ' is-paused' : ''}`} aria-labelledby={headingId}>
      <header className="scanner-group-head">
        <h3 id={headingId}>{name}</h3>
        <Login platform={platform} />
        <span className="scanner-group-switch">
          {paused && (
            <span className="paused-tag" aria-hidden="true">
              Paused
            </span>
          )}
          <Switch
            label={name}
            hideLabel
            on={running}
            disabled={false}
            saving={!!saving[group.pause]}
            onChange={(on) => save(group.pause, !on, name)}
          />
        </span>
        {scan.separate && (
          <div className="scanner-group-bar">
            <IntervalInput
              label="Every"
              context={`${name} scan`}
              setting={SCAN_INTERVAL_SETTING[platform]}
              value={intervals?.[SCAN_INTERVAL_SETTING[platform]]}
              onSaved={onIntervalSaved}
            />
            {runs && <PlatformSchedule status={runs} platform={platform} />}
            <RunNowButton
              state={scan.runStates[platform]}
              platform={platform}
              onRun={() => void scan.runNow(platform)}
            />
          </div>
        )}
      </header>
      {scan.separate && group.searchInterval && (
        <div className="scanner-group-search">
          <IntervalInput
            label="Search every"
            context={name}
            setting={group.searchInterval}
            value={intervals?.[group.searchInterval]}
            onSaved={onIntervalSaved}
          />
        </div>
      )}
      <ul className="feature-list">
        {group.features.map((f) => (
          <li key={f.kind} className="feature-row">
            <Switch
              label={f.label}
              on={switchValue(values, f.setting, !!f.inverted)}
              disabled={paused}
              saving={!!saving[f.setting]}
              onChange={(on) => save(f.setting, f.inverted ? !on : on, `${name} ${f.label}`)}
            />
            <RunStatus run={runs?.latest.find((r) => r?.kind === f.kind) ?? undefined} />
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Collapsed by default: per-platform schedule, Run now and switches, with each scanner's last run. */
function ScannerBody({ scan }: { scan: ScanStatusValue }) {
  const settings = useSettingSwitches()
  const { query } = scan
  const intervalSaved = () => {
    settings.query.reload()
    query.reload()
  }

  return (
    <div className="scanner-body">
      <InlineError message={settings.error} onDismiss={settings.clearError} />
      {settings.query.error !== undefined && !settings.query.data && (
        <ErrorState error={settings.query.error} onRetry={settings.query.reload} compact />
      )}
      {query.error !== undefined && !query.data && <ErrorState error={query.error} onRetry={query.reload} compact />}
      <div className="scanner-groups">
        {GROUPS.map((group) => (
          <GroupCard
            key={group.platform}
            group={group}
            settings={settings}
            scan={scan}
            onIntervalSaved={intervalSaved}
          />
        ))}
      </div>
    </div>
  )
}

export function ScannerPanel({ scan }: { scan: ScanStatusValue }) {
  const { query, separate, runStates, runNow, error, clearError } = scan
  const status = query.data

  return (
    <>
      <CollapsibleSection
        id="dashboard.scanner"
        title="Scanner"
        defaultOpen={false}
        className="scanner"
        meta={status ? separate ? <ScanSummary status={status} /> : <Schedule status={status} /> : undefined}
        actions={separate ? undefined : <RunNowButton state={runStates.all} onRun={() => void runNow()} />}
      >
        <ScannerBody scan={scan} />
      </CollapsibleSection>
      {/* Outside the section so a failed "Run now" shows while it is closed too. */}
      <InlineError message={error} onDismiss={clearError} />
    </>
  )
}
