import { useId, useState, type FormEvent } from 'react'
import { CONNECTION_NOTE_MAX, TEMPLATE_KINDS, type Template, type TemplateInput, type TemplateKind } from '../api/types'
import type { Action } from '../hooks/useAction'
import { charCount, TEMPLATE_KIND_SINGULAR } from '../lib/format'
import { sameInput, toInput, usesSubject, type TemplateDraft } from '../lib/templates'
import { InlineError } from './DataState'

interface KindText {
  name: string
  namePlaceholder: string
  subject: string
  subjectPlaceholder: string
  body: string
  bodyPlaceholder: string
  rows: number
}

const FORM_TEXT: Record<TemplateKind, KindText> = {
  answer: {
    name: 'Question',
    namePlaceholder: 'e.g. What is your notice period?',
    subject: '',
    subjectPlaceholder: '',
    body: 'Answer',
    bodyPlaceholder: '',
    rows: 5,
  },
  linkedin_message: {
    name: 'Name',
    namePlaceholder: 'e.g. Connection request: openings at [Company]',
    subject: 'Subject (InMail)',
    subjectPlaceholder: '',
    body: 'Message',
    bodyPlaceholder: 'Hi [Name], …',
    rows: 6,
  },
  email: {
    name: 'Name',
    namePlaceholder: 'e.g. Cold email to a hiring manager',
    subject: 'Subject',
    subjectPlaceholder: 'e.g. Application: [Role] at [Company]',
    body: 'Email body',
    bodyPlaceholder: 'Hi [Name], …',
    rows: 10,
  },
}

/** Character count for a LinkedIn message, with a note when it is too long for a connection note. */
export function MessageLength({ text, id, live = false }: { text: string; id?: string; live?: boolean }) {
  const length = charCount(text)
  const over = length > CONNECTION_NOTE_MAX
  return (
    <p id={id} className={`length-note${over ? ' length-note-over' : ''}`}>
      <span className="num">
        {length}/{CONNECTION_NOTE_MAX} characters
      </span>
      {/* Only the note is announced (not every keystroke's count). */}
      <span aria-live={live ? 'polite' : undefined}>
        {over ? ' · too long for a connection note' : ''}
      </span>
    </p>
  )
}

interface TemplateFormProps {
  initial: TemplateDraft
  action: Action<[TemplateInput], Template>
  submitLabel: string
  pendingLabel: string
  /** Edit forms: keep Save disabled until something changed. */
  requireChange?: boolean
  /** Put the cursor in the text box instead of the question (for answering an empty template). */
  focusBody?: boolean
  onDone: (saved: Template) => void
  onCancel: () => void
  className?: string
}

/** Add / edit form for a Template Library entry: kind, question or name, subject, text. */
export function TemplateForm({
  initial,
  action,
  submitLabel,
  pendingLabel,
  requireChange = false,
  focusBody = false,
  onDone,
  onCancel,
  className = 'add-form',
}: TemplateFormProps) {
  const [draft, setDraft] = useState(initial)
  const lengthId = useId()
  const text = FORM_TEXT[draft.kind]
  const busy = action.pending
  const input = toInput(draft)
  const canSubmit = !busy && input.question !== '' && (!requireChange || !sameInput(input, toInput(initial)))
  const isMessage = draft.kind === 'linkedin_message'

  const set = <K extends keyof TemplateDraft>(key: K, value: TemplateDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }))

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!canSubmit) return
    const result = await action.run(input)
    if (result.ok) onDone(result.value)
  }

  return (
    <form className={className} onSubmit={onSubmit}>
      <div className="form-grid">
        <label className="field">
          <span>Type</span>
          <select value={draft.kind} onChange={(e) => set('kind', e.target.value as TemplateKind)} disabled={busy}>
            {TEMPLATE_KINDS.map((k) => (
              <option key={k} value={k}>
                {TEMPLATE_KIND_SINGULAR[k]}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{text.name}</span>
          <input
            type="text"
            value={draft.question}
            onChange={(e) => set('question', e.target.value)}
            disabled={busy}
            placeholder={text.namePlaceholder}
            required
            autoFocus={!focusBody}
          />
        </label>
      </div>

      {usesSubject(draft.kind) && (
        <label className="field">
          <span>{text.subject}</span>
          <input
            type="text"
            value={draft.subject}
            onChange={(e) => set('subject', e.target.value)}
            disabled={busy}
            placeholder={text.subjectPlaceholder || undefined}
          />
        </label>
      )}

      <label className="field">
        <span>{text.body}</span>
        <textarea
          className="template-textarea"
          value={draft.answer}
          onChange={(e) => set('answer', e.target.value)}
          rows={text.rows}
          disabled={busy}
          placeholder={text.bodyPlaceholder || undefined}
          autoFocus={focusBody}
          aria-describedby={isMessage ? lengthId : undefined}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit()
          }}
        />
      </label>
      {isMessage && <MessageLength id={lengthId} text={input.answer} live />}

      <InlineError message={action.error} onDismiss={action.clearError} />
      <div className="btn-row form-actions">
        <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
          {busy ? pendingLabel : submitLabel}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}
