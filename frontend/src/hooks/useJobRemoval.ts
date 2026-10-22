import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../api/client'
import type { Job, JobStatusUpdateValue, ListResponse } from '../api/types'
import { isApplied, REMOVED_REASON } from '../lib/jobs'

/** A job removed with the × button, or put back with Undo. */
interface Entry {
  /** `removing`: being saved; `removed`: saved; `restoring`: Undo was clicked. */
  phase: 'removing' | 'removed' | 'restoring'
  /** The row as it was shown. A restored row shows it until the reloaded list has the job again. */
  job: Job
  /** `updated_at` of the listed job when removed. A listed job that changed since is shown again. */
  base: string
  /** Its position in the list, where Undo puts it back. */
  index: number
  /** What Undo sets: `applied` for an Applied row, `found` (Pending) otherwise. */
  restoreTo: JobStatusUpdateValue
  /** Set once Undo is saved: the list it was put back into. Any later list has the job itself. */
  savedOver?: Job[]
}

/** The "Removed · Undo" toast of the latest removal. */
export interface RemovedToastState {
  id: number
  /** New for every toast, so its timer starts over. */
  key: number
  /** Set when Undo was not saved (the job stays removed; Undo can be tried again). */
  error?: string
}

export interface JobRemoval {
  /** The list as shown: removed rows left out, restored rows put back, `total` to match. */
  view: (list: ListResponse<Job>) => ListResponse<Job>
  /**
   * Removes a row at once and saves it as skipped. `listed` is the job from the list, `shown` the
   * row as displayed (its status may have just been toggled). False when the row can't be removed now.
   */
  remove: (listed: Job, shown: Job, index: number) => boolean
  /** True while Undo of this job is being saved. */
  busy: (id: number) => boolean
  /** Why a removal was not saved, by job id (the row is shown again). */
  errors: Record<number, string>
  toast: RemovedToastState | null
  undo: () => void
  dismiss: () => void
}

const isBusy = (e: Entry | undefined) => e?.phase === 'restoring' && !e.savedOver

/**
 * Remove (×) with Undo for a job list. `items` is the list's current page; `onChange` runs after
 * each saved removal or Undo, to reload the list and its counts.
 */
export function useJobRemoval(items: Job[] | undefined, onChange: () => void): JobRemoval {
  const [entries, setEntries] = useState<Record<number, Entry>>({})
  const [errors, setErrors] = useState<Record<number, string>>({})
  const [toast, setToast] = useState<RemovedToastState | null>(null)

  // The async steps below read the latest values, not the ones of the render that started them.
  const entriesRef = useRef(entries)
  const itemsRef = useRef(items)
  const onChangeRef = useRef(onChange)
  useEffect(() => {
    itemsRef.current = items
    onChangeRef.current = onChange
  })
  const removals = useRef(new Map<number, Promise<boolean>>())
  const toastKey = useRef(0)

  const update = (id: number, fn: (e: Entry | undefined) => Entry | undefined) => {
    const next = { ...entriesRef.current }
    const entry = fn(next[id])
    if (entry) next[id] = entry
    else delete next[id]
    entriesRef.current = next
    setEntries(next)
  }

  const clearError = (id: number) =>
    setErrors((e) => {
      if (!(id in e)) return e
      const rest = { ...e }
      delete rest[id]
      return rest
    })

  const view = (list: ListResponse<Job>): ListResponse<Job> => {
    const listed = new Set(list.items.map((j) => j.id))
    const shown = list.items.filter((j) => {
      const e = entries[j.id]
      return !e || e.phase === 'restoring' || e.base !== j.updated_at
    })
    const back = Object.values(entries)
      .filter((e) => e.phase === 'restoring' && !listed.has(e.job.id) && (!e.savedOver || e.savedOver === list.items))
      .sort((a, b) => a.index - b.index)
    for (const e of back) shown.splice(Math.min(e.index, shown.length), 0, e.job)
    return { items: shown, total: Math.max(0, list.total + shown.length - list.items.length) }
  }

  const remove = (listed: Job, shown: Job, index: number): boolean => {
    const id = listed.id
    const prev = entriesRef.current[id]
    if (prev?.phase === 'removing' || isBusy(prev)) return false
    update(id, () => ({
      phase: 'removing',
      job: shown,
      base: listed.updated_at,
      index,
      restoreTo: isApplied(shown) ? 'applied' : 'found',
    }))
    clearError(id)
    toastKey.current += 1
    setToast({ id, key: toastKey.current })

    const saving = api.updateJob(id, { status: 'skipped', status_reason: REMOVED_REASON }).then(
      () => {
        // Undo clicked meanwhile: it saves the status and reloads.
        if (entriesRef.current[id]?.phase !== 'removing') return true
        update(id, (e) => e && { ...e, phase: 'removed' })
        onChangeRef.current()
        return true
      },
      (err: unknown) => {
        const undone = entriesRef.current[id]?.phase === 'restoring'
        update(id, () => undefined)
        setToast((t) => (t?.id === id ? null : t))
        if (!undone) setErrors((e) => ({ ...e, [id]: errorMessage(err) }))
        return false
      },
    )
    removals.current.set(id, saving)
    return true
  }

  const undo = () => {
    if (!toast) return
    const { id } = toast
    const entry = entriesRef.current[id]
    if (!entry || entry.phase === 'restoring') return
    setToast(null)
    update(id, () => ({ ...entry, phase: 'restoring', savedOver: undefined }))

    void (async () => {
      // A removal that didn't save leaves the job as it was: nothing to undo.
      if (!(await removals.current.get(id))) return
      try {
        const saved = await api.updateJob(id, { status: entry.restoreTo })
        update(id, (e) => (e?.phase === 'restoring' ? { ...e, job: saved, savedOver: itemsRef.current ?? [] } : e))
        onChangeRef.current()
      } catch (err) {
        update(id, (e) => e && { ...e, phase: 'removed' })
        toastKey.current += 1
        setToast({ id, key: toastKey.current, error: errorMessage(err) })
      }
    })()
  }

  const dismiss = useCallback(() => setToast(null), [])

  return {
    view,
    remove,
    busy: (id) => isBusy(entries[id]),
    errors,
    toast,
    undo,
    dismiss,
  }
}
