import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage } from '../api/client'
import { PLATFORMS, SCAN_KINDS_BY_PLATFORM, type Platform, type ScanStatus } from '../api/types'
import { PLATFORM_LABEL } from '../lib/format'
import { usePolling } from './usePolling'
import { useQuery, type QueryResult } from './useQuery'

const RUNNING_POLL_MS = 10_000
const IDLE_POLL_MS = 60_000
/** After "Run now", poll fast for this long even if the status doesn't say `running` yet. */
const START_GRACE_MS = 30_000

/** What a "Run now" starts: one platform's scan, or (older backends) the one scan of both. */
export type RunTarget = Platform | 'all'
const RUN_TARGETS: readonly RunTarget[] = [...PLATFORMS, 'all']

/** `requesting`: POST /api/scans/run is in flight. `starting`: accepted, not shown running yet. */
export type RunState = 'idle' | 'requesting' | 'starting' | 'running'

/** Changes whenever one of the target's scanners finishes a run (id + finish time of each). */
function finishedSignature(status: ScanStatus, target: RunTarget): string {
  const kinds = target === 'all' ? undefined : SCAN_KINDS_BY_PLATFORM[target]
  return status.latest
    .filter((run) => run?.finished_at && (!kinds || kinds.includes(run.kind)))
    .map((run) => `${run?.id}@${run?.finished_at}`)
    .join('|')
}

/** Whether the target's scan is running. Without `platforms` (older backend) only the top-level flag is known. */
function isRunning(status: ScanStatus | undefined, target: RunTarget): boolean {
  if (!status) return false
  if (target === 'all') return status.running
  return status.platforms?.[target]?.running ?? status.running
}

/** `map` without `keys`; the same object when it has none of them (so React skips the update). */
function without<V>(map: Partial<Record<RunTarget, V>>, keys: readonly RunTarget[]): Partial<Record<RunTarget, V>> {
  if (!keys.some((key) => key in map)) return map
  const next = { ...map }
  for (const key of keys) delete next[key]
  return next
}

export interface ScanStatusValue {
  query: QueryResult<ScanStatus>
  /**
   * false once the status shows the backend predates separate LinkedIn and Naukri scans (no
   * `platforms`): then there is one schedule and one "Run now" (target `all`).
   */
  separate: boolean
  /** Any scan is running. */
  running: boolean
  /** State of each "Run now". */
  runStates: Record<RunTarget, RunState>
  error: string | null
  clearError: () => void
  /** Starts that platform's scan, or without one, every platform's. */
  runNow: (platform?: Platform) => Promise<void>
}

/**
 * GET /api/scans/status, polled every 10 s while a scan is going and every 60 s otherwise.
 * `onRunFinished` is called whenever a scanner finishes (so lists and counts can refresh).
 */
export function useScanStatus(onRunFinished: () => void): ScanStatusValue {
  const query = useQuery('scans:status', (signal) => api.scanStatus(signal))
  const { data, reload } = query
  const running = data?.running ?? false

  /** When each accepted "Run now" was accepted, until the status shows the scan running. */
  const [starting, setStarting] = useState<Partial<Record<RunTarget, number>>>({})
  // Once the scan shows up, the normal "running" polling takes over.
  const shown = RUN_TARGETS.filter((t) => starting[t] !== undefined && isRunning(data, t))
  if (shown.length) setStarting((s) => without(s, shown))
  useEffect(() => {
    const times = Object.values(starting)
    if (!times.length) return
    const wait = Math.max(0, Math.min(...times) + START_GRACE_MS - Date.now())
    const timer = window.setTimeout(() => {
      const now = Date.now()
      setStarting((s) => without(s, RUN_TARGETS.filter((t) => (s[t] ?? now) + START_GRACE_MS <= now)))
    }, wait)
    return () => window.clearTimeout(timer)
  }, [starting])

  const waiting = Object.keys(starting).length > 0
  usePolling(query, running || waiting ? RUNNING_POLL_MS : IDLE_POLL_MS)

  const onFinishedRef = useRef(onRunFinished)
  useEffect(() => {
    onFinishedRef.current = onRunFinished
  })
  // One signature per target, so a quick scan that never showed as running still ends its "starting".
  const signatures = data ? RUN_TARGETS.map((t) => finishedSignature(data, t)).join('\n') : undefined
  const lastSignatures = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (signatures === undefined) return
    const previous = lastSignatures.current?.split('\n')
    lastSignatures.current = signatures
    if (!previous) return
    const current = signatures.split('\n')
    const finished = RUN_TARGETS.filter((_, i) => previous[i] !== current[i])
    if (!finished.length) return
    setStarting((s) => without(s, finished))
    onFinishedRef.current()
  }, [signatures])

  const [requesting, setRequesting] = useState<Partial<Record<RunTarget, true>>>({})
  const [error, setError] = useState<string | null>(null)
  const runNow = useCallback(
    async (platform?: Platform) => {
      const target: RunTarget = platform ?? 'all'
      const name = platform ? PLATFORM_LABEL[platform] : undefined
      setRequesting((r) => ({ ...r, [target]: true }))
      setError(null)
      try {
        await api.runScan(platform)
        setStarting((s) => ({ ...s, [target]: Date.now() }))
      } catch (err) {
        setError(
          err instanceof ApiError && err.status === 409
            ? `${name ? `${name} scan` : 'A scan'} is already running.`
            : name
              ? `${name}: ${errorMessage(err)}`
              : errorMessage(err),
        )
      } finally {
        setRequesting((r) => without(r, [target]))
        reload()
      }
    },
    [reload],
  )
  const clearError = useCallback(() => setError(null), [])

  const stateOf = (target: RunTarget): RunState =>
    isRunning(data, target)
      ? 'running'
      : requesting[target]
        ? 'requesting'
        : starting[target] !== undefined
          ? 'starting'
          : 'idle'
  const runStates: Record<RunTarget, RunState> = {
    linkedin: stateOf('linkedin'),
    naukri: stateOf('naukri'),
    all: stateOf('all'),
  }

  return {
    query,
    separate: data === undefined || data.platforms !== undefined,
    running,
    runStates,
    error,
    clearError,
    runNow,
  }
}
