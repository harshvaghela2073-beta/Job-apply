import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { api } from '../api/client'
import type { Profile, ProfileFieldInput } from '../api/types'
import { useAction } from '../hooks/useAction'
import { useProfile } from '../hooks/useProfile'
import { fieldValue, findField, joinList, splitList } from '../lib/profile'
import { Collapsible } from './Collapsible'
import { ErrorState, InlineError, Loading } from './DataState'
import { ArrowDownIcon, ArrowUpIcon, CloseIcon } from './Icons'
import '../templates-profile.css'

/** Section and labels for a list field that doesn't exist yet (existing fields keep theirs). */
const SECTION = 'Job search priority'

const GROUPS = [
  { key: 'search_experience_order', title: 'Experience', item: 'experience level', label: 'Experience (in order)' },
  { key: 'search_roles', title: 'Roles', item: 'role', label: 'Roles (in order)' },
  { key: 'search_locations', title: 'Locations', item: 'location', label: 'Locations' },
] as const

type Group = (typeof GROUPS)[number]
type GroupKey = Group['key']
type Lists<T> = Record<GroupKey, T>

interface Item {
  id: number
  text: string
}

const NO_TEXT: Lists<string> = { search_experience_order: '', search_roles: '', search_locations: '' }

function savedLists(profile: Profile): Lists<string[]> {
  const list = (key: GroupKey) => splitList(fieldValue(profile, key))
  return {
    search_experience_order: list('search_experience_order'),
    search_roles: list('search_roles'),
    search_locations: list('search_locations'),
  }
}

/** The list as it would be saved: items trimmed and split on commas, plus what's typed in the Add box. */
function finalList(items: Item[], pending: string): string[] {
  const list = items.flatMap((item) => splitList(item.text))
  const seen = new Set(list.map((t) => t.toLowerCase()))
  for (const text of splitList(pending)) {
    if (!seen.has(text.toLowerCase())) list.push(text)
    seen.add(text.toLowerCase())
  }
  return list
}

function problemWith(group: Group, list: string[], before: string[]): string | null {
  if (list.length === 0 && before.length > 0) return `${group.title}: add at least one ${group.item}.`
  const seen = new Set<string>()
  for (const text of list) {
    if (seen.has(text.toLowerCase())) return `${group.title}: "${text}" is listed twice.`
    seen.add(text.toLowerCase())
  }
  return null
}

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const focusAttr = (value: string) => `[data-focus="${value}"]`

function PriorityLists({ profile }: { profile: Profile }) {
  const { replace } = useProfile()
  const saved = savedLists(profile)
  const save = useAction((fields: ProfileFieldInput[]) => api.updateProfileFields({ fields }))

  /** null while viewing; the lists being edited otherwise. */
  const [draft, setDraft] = useState<Lists<Item[]> | null>(null)
  const [newText, setNewText] = useState(NO_TEXT)
  const [addError, setAddError] = useState<{ key: GroupKey; message: string } | null>(null)
  const [savedNote, setSavedNote] = useState(false)
  const [announcement, setAnnouncement] = useState('')

  const nextId = useRef(0)
  const rootRef = useRef<HTMLDivElement>(null)
  // A selector to focus after the next render (buttons move or disappear when the list changes).
  const pendingFocus = useRef<string | null>(null)

  useEffect(() => {
    const selector = pendingFocus.current
    if (!selector) return
    pendingFocus.current = null
    rootRef.current?.querySelector<HTMLElement>(selector)?.focus()
  })

  useEffect(() => {
    if (!savedNote) return
    const timer = window.setTimeout(() => setSavedNote(false), 2000)
    return () => window.clearTimeout(timer)
  }, [savedNote])

  const toItems = (texts: string[]): Item[] => texts.map((text) => ({ id: nextId.current++, text }))

  const startEdit = () => {
    save.clearError()
    setAddError(null)
    setSavedNote(false)
    setNewText(NO_TEXT)
    setDraft({
      search_experience_order: toItems(saved.search_experience_order),
      search_roles: toItems(saved.search_roles),
      search_locations: toItems(saved.search_locations),
    })
    pendingFocus.current = '.sp-edit input'
  }

  const stopEdit = () => {
    setDraft(null)
    setNewText(NO_TEXT)
    setAddError(null)
    pendingFocus.current = focusAttr('edit')
  }

  const cancel = () => {
    save.clearError()
    stopEdit()
  }

  const setList = (key: GroupKey, items: Item[]) => setDraft((d) => (d ? { ...d, [key]: items } : d))

  const editItem = (key: GroupKey, index: number, text: string) => {
    if (!draft) return
    setList(
      key,
      draft[key].map((item, i) => (i === index ? { ...item, text } : item)),
    )
  }

  const move = (key: GroupKey, index: number, delta: -1 | 1, keepFocusOnText = false) => {
    if (!draft) return
    const items = [...draft[key]]
    const to = index + delta
    if (to < 0 || to >= items.length) return
    const [item] = items.splice(index, 1)
    items.splice(to, 0, item)
    setList(key, items)
    // Keep focus on the moved item; at either end that arrow is disabled, so use the other one.
    const atEnd = delta < 0 ? to === 0 : to === items.length - 1
    const arrow = delta < 0 ? (atEnd ? 'down' : 'up') : atEnd ? 'up' : 'down'
    pendingFocus.current = focusAttr(`${item.id}:${keepFocusOnText ? 'text' : arrow}`)
    setAnnouncement(`${item.text || 'Item'} moved to position ${to + 1} of ${items.length}.`)
  }

  const remove = (key: GroupKey, index: number) => {
    if (!draft) return
    const removed = draft[key][index]
    const items = draft[key].filter((_, i) => i !== index)
    setList(key, items)
    const neighbour = items[index] ?? items[index - 1]
    pendingFocus.current = focusAttr(neighbour ? `${neighbour.id}:remove` : `add:${key}`)
    setAnnouncement(`${removed.text || 'Item'} removed.`)
  }

  const add = (key: GroupKey) => {
    if (!draft) return
    const typed = splitList(newText[key])
    if (typed.length === 0) return
    const seen = new Set(finalList(draft[key], '').map((t) => t.toLowerCase()))
    const fresh: string[] = []
    for (const text of typed) {
      if (!seen.has(text.toLowerCase())) fresh.push(text)
      seen.add(text.toLowerCase())
    }
    if (fresh.length === 0) {
      setAddError({ key, message: 'Already in the list.' })
      return
    }
    setList(key, [...draft[key], ...toItems(fresh)])
    setNewText((t) => ({ ...t, [key]: '' }))
    setAddError(null)
    setAnnouncement(`${fresh.join(', ')} added.`)
  }

  const onItemKeyDown = (key: GroupKey, index: number) => (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault()
      move(key, index, e.key === 'ArrowUp' ? -1 : 1, true)
    }
  }

  const lists: Lists<string[]> | null = draft && {
    search_experience_order: finalList(draft.search_experience_order, newText.search_experience_order),
    search_roles: finalList(draft.search_roles, newText.search_roles),
    search_locations: finalList(draft.search_locations, newText.search_locations),
  }
  const changed = lists ? GROUPS.filter((g) => joinList(lists[g.key]) !== joinList(saved[g.key])) : []
  const problem = lists
    ? (GROUPS.map((g) => problemWith(g, lists[g.key], saved[g.key])).find((p) => p !== null) ?? null)
    : null
  const busy = save.pending

  const onSave = async () => {
    if (!lists || changed.length === 0 || problem) return
    const fields: ProfileFieldInput[] = changed.map((g) => {
      const field = findField(profile, g.key)
      return {
        key: g.key,
        label: field?.label ?? g.label,
        section: field?.section ?? SECTION,
        value: joinList(lists[g.key]),
      }
    })
    const result = await save.run(fields)
    if (!result.ok) return
    replace(result.value)
    stopEdit()
    setSavedNote(true)
  }

  return (
    <div ref={rootRef} className="sp">
      {draft ? (
        <div className="sp-groups sp-edit">
          {GROUPS.map((g) => {
            const items = draft[g.key]
            return (
              <fieldset key={g.key} className="sp-group sp-fieldset" disabled={busy}>
                <legend className="sp-group-title">{g.title}</legend>
                {items.length > 0 && (
                  <ol className="sp-edit-list">
                    {items.map((item, i) => {
                      const name = item.text.trim() || g.item
                      return (
                        <li key={item.id} className="sp-edit-item">
                          <input
                            type="text"
                            value={item.text}
                            data-focus={`${item.id}:text`}
                            aria-label={`${capitalize(g.item)} ${i + 1} of ${items.length}`}
                            onChange={(e) => editItem(g.key, i, e.target.value)}
                            onKeyDown={onItemKeyDown(g.key, i)}
                          />
                          <button
                            type="button"
                            className="tp-icon-btn"
                            data-focus={`${item.id}:up`}
                            aria-label={`Move ${name} up`}
                            title="Move up"
                            disabled={i === 0}
                            onClick={() => move(g.key, i, -1)}
                          >
                            <ArrowUpIcon />
                          </button>
                          <button
                            type="button"
                            className="tp-icon-btn"
                            data-focus={`${item.id}:down`}
                            aria-label={`Move ${name} down`}
                            title="Move down"
                            disabled={i === items.length - 1}
                            onClick={() => move(g.key, i, 1)}
                          >
                            <ArrowDownIcon />
                          </button>
                          <button
                            type="button"
                            className="tp-icon-btn tp-icon-btn-danger"
                            data-focus={`${item.id}:remove`}
                            aria-label={`Remove ${name}`}
                            title="Remove"
                            onClick={() => remove(g.key, i)}
                          >
                            <CloseIcon />
                          </button>
                        </li>
                      )
                    })}
                  </ol>
                )}
                <div className="sp-add">
                  <input
                    type="text"
                    value={newText[g.key]}
                    data-focus={`add:${g.key}`}
                    placeholder={`Add ${g.item}`}
                    aria-label={`Add ${g.item}`}
                    aria-invalid={addError?.key === g.key || undefined}
                    onChange={(e) => {
                      const text = e.target.value
                      setNewText((t) => ({ ...t, [g.key]: text }))
                      if (addError?.key === g.key) setAddError(null)
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault()
                        add(g.key)
                      }
                    }}
                  />
                  <button type="button" className="btn btn-sm" onClick={() => add(g.key)} disabled={!newText[g.key].trim()}>
                    Add
                  </button>
                </div>
                {addError?.key === g.key && (
                  <p className="sp-error" role="alert">
                    {addError.message}
                  </p>
                )}
              </fieldset>
            )
          })}
        </div>
      ) : (
        <div className="sp-groups">
          {GROUPS.map((g) => (
            <div key={g.key} className="sp-group">
              <h3 className="sp-group-title">{g.title}</h3>
              {saved[g.key].length > 0 ? (
                <ol className="sp-chips">
                  {saved[g.key].map((text, i) => (
                    <li key={`${i}:${text}`}>
                      <span className="chip">{text}</span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="muted small">Not set</p>
              )}
            </div>
          ))}
        </div>
      )}

      {draft && problem && (
        <p className="sp-error" role="alert">
          {problem}
        </p>
      )}
      <InlineError message={save.error} onDismiss={save.clearError} />

      <div className="sp-foot">
        {draft ? (
          <>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={onSave}
              disabled={busy || changed.length === 0 || problem !== null}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={cancel} disabled={busy}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-sm" data-focus="edit" onClick={startEdit}>
            Edit
          </button>
        )}
        <span className="sp-saved" role="status">
          {savedNote ? 'Saved' : ''}
        </span>
      </div>
      <span className="sr-only" role="status">
        {announcement}
      </span>
    </div>
  )
}

/** The job-search priority (`search_*` profile fields): three ordered lists, editable in place. */
export function SearchPriority() {
  const profile = useProfile()
  return (
    <Collapsible id="search-priority" title="Job search priority">
      {profile.data ? (
        <PriorityLists profile={profile.data} />
      ) : profile.error ? (
        <ErrorState error={profile.error} onRetry={profile.reload} compact />
      ) : (
        <Loading label="Loading…" />
      )}
    </Collapsible>
  )
}
