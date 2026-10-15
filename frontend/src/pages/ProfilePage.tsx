import { useEffect, useId, useRef, useState } from 'react'
import type { Profile, ProfileField, ProfileSection } from '../api/types'
import { Avatar } from '../components/Avatar'
import { EmptyState, QueryBoundary } from '../components/DataState'
import { ExternalLink } from '../components/ExternalLink'
import { PencilIcon } from '../components/Icons'
import { LinkifiedText } from '../components/LinkifiedText'
import { ProfileFieldEditor } from '../components/ProfileFieldEditor'
import { useProfile } from '../hooks/useProfile'
import { displayUrl, findField, isEmail, isUrl, splitList, telHref } from '../lib/profile'
import '../templates-profile.css'
import { ResumeLink } from '../components/ResumeLink'

function FieldValue({ field }: { field: ProfileField }) {
  const value = field.value.trim()
  if (!value) return <span className="muted">Not added</span>
  if (field.key === 'skills') {
    return (
      <ul className="chips chips-sm">
        {splitList(value).map((skill, i) => (
          <li key={`${i}:${skill}`} className="chip">
            {skill}
          </li>
        ))}
      </ul>
    )
  }
  if (isEmail(value)) return <a href={`mailto:${value}`}>{value}</a>
  if (field.key === 'phone') return <a href={telHref(value)}>{value}</a>
  if (isUrl(value)) {
    return (
      <ExternalLink href={value} className="break-all">
        {displayUrl(value)} ↗
      </ExternalLink>
    )
  }
  return <LinkifiedText text={field.value} />
}

function EditButton({ field, onClick }: { field: ProfileField; onClick: () => void }) {
  return (
    <button
      type="button"
      className="tp-icon-btn pf-edit"
      data-edit-key={field.key}
      aria-label={`Edit ${field.label}`}
      title="Edit"
      onClick={onClick}
    >
      <PencilIcon />
    </button>
  )
}

interface SectionBlockProps {
  section: ProfileSection
  editing: string | null
  onEdit: (key: string) => void
  onClose: (key: string) => void
}

function SectionBlock({ section, editing, onEdit, onClose }: SectionBlockProps) {
  const titleId = useId()
  return (
    <section className="pf-section" aria-labelledby={titleId}>
      <h2 id={titleId} className="pf-section-title">
        {section.title}
      </h2>
      <dl className="pf-rows">
        {section.fields.map((f) => (
          <div key={f.key} className={`pf-row${editing === f.key ? ' pf-row-editing' : ''}`}>
            <dt className="pf-label">{f.label}</dt>
            <dd className="pf-value">
              {editing === f.key ? (
                <ProfileFieldEditor field={f} onClose={() => onClose(f.key)} />
              ) : (
                <>
                  <div className="pf-value-text">
                    <FieldValue field={f} />
                  </div>
                  <EditButton field={f} onClick={() => onEdit(f.key)} />
                </>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function ProfileView({ profile }: { profile: Profile }) {
  const s = profile.summary
  /** Key of the field being edited. */
  const [editing, setEditing] = useState<string | null>(null)

  // Focus returns to the field's edit button when its editor closes.
  const rootRef = useRef<HTMLDivElement>(null)
  const pendingFocus = useRef<string | null>(null)
  useEffect(() => {
    const key = pendingFocus.current
    if (!key) return
    pendingFocus.current = null
    rootRef.current?.querySelector<HTMLElement>(`[data-edit-key="${CSS.escape(key)}"]`)?.focus()
  })

  const close = (key: string) => {
    setEditing(null)
    pendingFocus.current = key
  }

  const nameField = findField(profile, 'name')
  // The name is the page heading, so it isn't repeated in the card.
  const sections = profile.sections
    .map((section) => ({ ...section, fields: section.fields.filter((f) => f.key !== 'name') }))
    .filter((section) => section.fields.length > 0)
  const name = s.name ?? 'Profile'

  return (
    <div className="pf" ref={rootRef}>
      <header className="pf-head">
        <Avatar name={s.name} size="lg" />
        {nameField && editing === nameField.key ? (
          <>
            <h1 className="sr-only">{name}</h1>
            <ProfileFieldEditor field={nameField} onClose={() => close(nameField.key)} className="pf-name-editor" />
          </>
        ) : (
          <div className="pf-name-row">
            <h1 className="pf-name">{name}</h1>
            {nameField && <EditButton field={nameField} onClick={() => setEditing(nameField.key)} />}
          </div>
        )}
      </header>

      {sections.length === 0 && !s.resume ? (
        <EmptyState title="No profile details yet." />
      ) : (
        <div className="pf-card">
          {sections.map((section) => (
            <SectionBlock
              key={section.title}
              section={section}
              editing={editing}
              onEdit={setEditing}
              onClose={close}
            />
          ))}
          {s.resume && (
            <dl className="pf-section pf-rows">
              <div className="pf-row">
                <dt className="pf-label">Resume</dt>
                <dd className="pf-value">
                  <div className="pf-value-text">
                    <ResumeLink url={s.resume.url} filename={s.resume.filename} className="break-all">
                      {s.resume.filename} ↗
                    </ResumeLink>
                  </div>
                </dd>
              </div>
            </dl>
          )}
        </div>
      )}
    </div>
  )
}

export function ProfilePage() {
  const profile = useProfile()
  return (
    <main className="page pf-page">
      <button
        type="button"
        className="tp-icon-btn pf-reload"
        onClick={profile.reload}
        disabled={profile.loading}
        aria-label="Reload profile"
        title="Reload"
      >
        <span aria-hidden="true">↻</span>
      </button>
      <QueryBoundary query={profile} loadingLabel="Loading profile…">
        {(p) => <ProfileView profile={p} />}
      </QueryBoundary>
    </main>
  )
}
