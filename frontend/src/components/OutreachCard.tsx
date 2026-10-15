import { useState } from 'react'
import { api } from '../api/client'
import { CONNECTION_NOTE_MAX, type OutreachMessage } from '../api/types'
import { useAction } from '../hooks/useAction'
import { charCount, formatDateTime, formatUtc, OUTREACH_KIND_LABEL } from '../lib/format'
import { Badge, OutreachStatusBadge } from './Badge'
import { InlineError } from './DataState'
import { ExpandableText } from './ExpandableText'
import { ExternalLink } from './ExternalLink'

function Time({ label, value }: { label: string; value: string | null }) {
  if (!value) return null
  return (
    <span>
      {label}{' '}
      <time dateTime={value} title={formatUtc(value)}>
        {formatDateTime(value)}
      </time>
    </span>
  )
}

interface OutreachCardProps {
  msg: OutreachMessage
  /** Smaller card for lists: one time, and the message cut to a few lines until expanded. */
  compact?: boolean
  onChanged: () => void
}

/** One message of the outreach queue: an email or LinkedIn note the tool sent or prepared. */
export function OutreachCard({ msg, compact = false, onChanged }: OutreachCardProps) {
  const [editing, setEditing] = useState(false)
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')

  const isEmail = msg.kind === 'email'
  const isNote = msg.kind === 'connection_note'

  const save = useAction(() => api.updateOutreach(msg.id, isEmail ? { subject, body } : { body }))
  const approve = useAction(() => api.approveOutreach(msg.id))
  const reject = useAction(() => api.rejectOutreach(msg.id))
  const busy = save.pending || approve.pending || reject.pending
  const error = save.error ?? approve.error ?? reject.error
  const clearErrors = () => {
    save.clearError()
    approve.clearError()
    reject.clearError()
  }

  const startEdit = () => {
    setSubject(msg.subject ?? '')
    setBody(msg.body)
    clearErrors()
    setEditing(true)
  }

  const bodyLength = charCount(editing ? body : msg.body)
  const overLimit = isNote && bodyLength > CONNECTION_NOTE_MAX
  const dirty = editing && (body !== msg.body || (isEmail && subject !== (msg.subject ?? '')))
  const emptyBody = editing && body.trim() === ''

  const onSave = async () => {
    const result = await save.run()
    if (result.ok) {
      setEditing(false)
      onChanged()
    }
  }
  const onApprove = async () => {
    if ((await approve.run()).ok) onChanged()
  }
  const onReject = async () => {
    if (!window.confirm('Reject this message? It will not be sent.')) return
    if ((await reject.run()).ok) onChanged()
  }

  const canEdit = msg.status === 'draft'
  const canApprove = msg.status === 'draft'
  const canReject = msg.status === 'draft' || msg.status === 'approved'

  return (
    <article className={`outreach-card outreach-${msg.status}${compact ? ' outreach-card-compact' : ''}`}>
      <header className="outreach-head">
        <div className="outreach-recipient">
          {msg.recipient_profile_url ? (
            <ExternalLink href={msg.recipient_profile_url} className="recipient-name">
              {msg.recipient_name || 'LinkedIn profile'} ↗
            </ExternalLink>
          ) : (
            <span className="recipient-name">{msg.recipient_name || 'Unknown recipient'}</span>
          )}
          {isEmail && msg.recipient_email && (
            <a href={`mailto:${msg.recipient_email}`} className="recipient-email">
              {msg.recipient_email}
            </a>
          )}
        </div>
        <div className="outreach-tags">
          <Badge tone="neutral">{OUTREACH_KIND_LABEL[msg.kind]}</Badge>
          <OutreachStatusBadge status={msg.status} />
        </div>
      </header>

      <div className="job-card-meta">
        {compact ? (
          msg.sent_at ? (
            <Time label="Sent" value={msg.sent_at} />
          ) : (
            <Time label="Created" value={msg.created_at} />
          )
        ) : (
          <>
            <Time label="Created" value={msg.created_at} />
            <Time label="Approved" value={msg.approved_at} />
            <Time label="Sent" value={msg.sent_at} />
            {msg.job_id !== null && <span>Job #{msg.job_id}</span>}
          </>
        )}
      </div>

      {editing ? (
        <div className="outreach-edit">
          {isEmail && (
            <label className="field">
              <span>Subject</span>
              <input type="text" value={subject} onChange={(e) => setSubject(e.target.value)} disabled={busy} />
            </label>
          )}
          <label className="field">
            <span>
              Message
              {isNote && (
                <span className={`char-count${overLimit ? ' over' : ''}`} aria-live="polite">
                  {bodyLength}/{CONNECTION_NOTE_MAX}
                </span>
              )}
            </span>
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={isNote ? 4 : 10}
              disabled={busy}
              aria-invalid={overLimit}
            />
          </label>
          {overLimit && (
            <p className="field-error">
              Connection notes can be at most {CONNECTION_NOTE_MAX} characters. Remove{' '}
              {bodyLength - CONNECTION_NOTE_MAX} to save.
            </p>
          )}
        </div>
      ) : (
        <div className="outreach-body">
          {isEmail && (
            <div className="outreach-subject">
              <span className="muted">Subject:</span> {msg.subject || <em className="muted">(no subject)</em>}
            </div>
          )}
          {compact ? <ExpandableText text={msg.body} lines={3} /> : <div className="message-text">{msg.body}</div>}
          {isNote && !compact && (
            <div className={`char-count${overLimit ? ' over' : ''}`}>
              {bodyLength}/{CONNECTION_NOTE_MAX}
            </div>
          )}
        </div>
      )}

      {msg.error && <p className="cell-note cell-note-error">Error: {msg.error}</p>}
      <InlineError message={error} onDismiss={clearErrors} />

      {(canEdit || canApprove || canReject) && (
        <footer className="btn-row">
          {editing ? (
            <>
              <button
                type="button"
                className="btn btn-primary"
                onClick={onSave}
                disabled={busy || !dirty || overLimit || emptyBody}
              >
                {save.pending ? 'Saving…' : 'Save'}
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)} disabled={busy}>
                Cancel
              </button>
            </>
          ) : (
            <>
              {canApprove && (
                <button type="button" className="btn btn-success" onClick={onApprove} disabled={busy || overLimit}>
                  {approve.pending ? 'Approving…' : 'Approve'}
                </button>
              )}
              {canEdit && (
                <button type="button" className="btn" onClick={startEdit} disabled={busy}>
                  Edit
                </button>
              )}
              {canReject && (
                <button type="button" className="btn btn-danger-ghost" onClick={onReject} disabled={busy}>
                  {reject.pending ? 'Rejecting…' : 'Reject'}
                </button>
              )}
            </>
          )}
        </footer>
      )}
    </article>
  )
}
