import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { api } from '../api/client'
import type { ProfileField } from '../api/types'
import { useAction } from '../hooks/useAction'
import { useProfile } from '../hooks/useProfile'
import { InlineError } from './DataState'
import '../templates-profile.css'

interface ProfileFieldEditorProps {
  field: ProfileField
  /** `saved` is true after a successful save, false on Cancel / Escape. */
  onClose: (saved: boolean) => void
  className?: string
}

/** Inline editor for one profile value; saves with PUT /api/profile/fields. */
export function ProfileFieldEditor({ field, onClose, className = '' }: ProfileFieldEditorProps) {
  const { replace } = useProfile()
  const [value, setValue] = useState(field.value)
  const save = useAction((next: string) =>
    api.updateProfileFields({
      fields: [{ key: field.key, label: field.label, section: field.section, value: next }],
    }),
  )

  const multiline = field.value.includes('\n') || field.value.length > 60
  const next = value.trim()
  const canSave = !save.pending && next !== '' && next !== field.value.trim()

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!canSave) return
    const result = await save.run(next)
    if (!result.ok) return
    replace(result.value)
    onClose(true)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLFormElement>) => {
    if (e.key === 'Escape' && !save.pending) {
      e.preventDefault()
      e.stopPropagation()
      onClose(false)
    }
  }

  return (
    <form className={`pf-editor${className ? ` ${className}` : ''}`} onSubmit={onSubmit} onKeyDown={onKeyDown}>
      {multiline ? (
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={Math.min(10, Math.max(3, value.split('\n').length + 1))}
          aria-label={field.label}
          disabled={save.pending}
          autoFocus
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit()
          }}
        />
      ) : (
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label={field.label}
          disabled={save.pending}
          autoFocus
        />
      )}
      <div className="pf-editor-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSave}>
          {save.pending ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onClose(false)} disabled={save.pending}>
          Cancel
        </button>
      </div>
      <InlineError message={save.error} onDismiss={save.clearError} />
    </form>
  )
}
