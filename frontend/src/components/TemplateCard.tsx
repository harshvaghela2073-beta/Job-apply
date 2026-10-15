import { useState } from 'react'
import { api } from '../api/client'
import type { Template, TemplateInput } from '../api/types'
import { useAction } from '../hooks/useAction'
import { draftFromTemplate } from '../lib/templates'
import { Badge } from './Badge'
import { CopyButton } from './CopyButton'
import { InlineError } from './DataState'
import { ExpandableText } from './ExpandableText'
import { MessageLength, TemplateForm } from './TemplateForm'

interface TemplateCardProps {
  template: Template
  /** Called after an edit (with the saved template) or a delete. */
  onChanged: (saved?: Template) => void
}

/** One Template Library entry: copy, edit in place, delete. */
export function TemplateCard({ template: t, onChanged }: TemplateCardProps) {
  const [editing, setEditing] = useState(false)
  const save = useAction((input: TemplateInput) => api.updateTemplate(t.id, input))
  const remove = useAction(() => api.deleteTemplate(t.id))

  const startEdit = () => {
    save.clearError()
    remove.clearError()
    setEditing(true)
  }

  const onDelete = async () => {
    if (!window.confirm(`Delete this template?\n\n"${t.question}"`)) return
    if ((await remove.run()).ok) onChanged()
  }

  if (editing) {
    return (
      <article className="template-card template-card-editing" aria-label={`Editing: ${t.question}`}>
        <TemplateForm
          className="template-edit"
          initial={draftFromTemplate(t)}
          action={save}
          submitLabel="Save"
          pendingLabel="Saving…"
          requireChange
          focusBody={!t.is_answered}
          onDone={(saved) => {
            setEditing(false)
            onChanged(saved)
          }}
          onCancel={() => setEditing(false)}
        />
      </article>
    )
  }

  const showSubject = t.kind === 'email' || !!t.subject
  return (
    <article className={`template-card${t.is_answered ? '' : ' template-unanswered'}`}>
      <header className="template-head">
        <h3 className="template-title">{t.question}</h3>
        <div className="btn-row template-actions">
          <CopyButton
            text={t.answer}
            label="Copy"
            what={t.question}
            disabled={!t.is_answered}
            className="btn btn-sm btn-copy"
          />
          <button type="button" className="btn btn-sm" onClick={startEdit} disabled={remove.pending}>
            {t.is_answered ? 'Edit' : 'Answer'}
          </button>
          <button type="button" className="btn btn-sm btn-danger-ghost" onClick={onDelete} disabled={remove.pending}>
            {remove.pending ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </header>

      {showSubject && (
        <div className="template-subject">
          <span className="muted">Subject:</span>{' '}
          {t.subject ? <span className="template-subject-text">{t.subject}</span> : <em className="muted">(no subject)</em>}
          {t.subject && <CopyButton text={t.subject} label="Copy subject" what={t.question} />}
        </div>
      )}

      <div className="template-body">
        {t.is_answered ? (
          <ExpandableText text={t.answer} />
        ) : (
          <Badge tone="warning" title={t.answer ? `Current value: ${t.answer}` : undefined}>
            No answer yet
          </Badge>
        )}
        {t.kind === 'linkedin_message' && t.is_answered && <MessageLength text={t.answer} />}
      </div>

      <InlineError message={remove.error} onDismiss={remove.clearError} />
    </article>
  )
}
