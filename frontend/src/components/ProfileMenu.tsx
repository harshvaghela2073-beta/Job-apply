import { Link } from 'react-router-dom'
import { errorMessage } from '../api/client'
import type { Profile } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { usePopover } from '../hooks/usePopover'
import { useProfile } from '../hooks/useProfile'
import { experienceText, firstName, hasValue, isEmptyProfile, joinDot, telHref } from '../lib/profile'
import { Avatar } from './Avatar'
import { ExternalLink } from './ExternalLink'
import { ResumeLink } from './ResumeLink'

function ProfileCard({ profile, onNavigate }: { profile: Profile; onNavigate: () => void }) {
  const s = profile.summary
  const role = joinDot(s.designation, s.current_company)
  const where = joinDot(s.location, s.total_experience && experienceText(s.total_experience))
  const hasLinks = hasValue(s.linkedin_url) || hasValue(s.naukri_url) || s.resume

  return (
    <>
      <div className="pop-identity">
        <Avatar name={s.name} size="md" />
        <div className="pop-identity-text">
          <p className="pop-name">{s.name ?? <span className="muted">Name not added</span>}</p>
          {role && <p className="pop-line">{role}</p>}
          {where && <p className="pop-line muted">{where}</p>}
        </div>
      </div>

      {(hasValue(s.phone) || hasValue(s.email) || hasValue(s.current_ctc)) && (
        <dl className="pop-facts">
          {hasValue(s.phone) && (
            <div>
              <dt>Phone</dt>
              <dd>
                <a href={telHref(s.phone)}>{s.phone}</a>
              </dd>
            </div>
          )}
          {hasValue(s.email) && (
            <div>
              <dt>Email</dt>
              <dd>
                <a href={`mailto:${s.email}`}>{s.email}</a>
              </dd>
            </div>
          )}
          {hasValue(s.current_ctc) && (
            <div>
              <dt>Current CTC</dt>
              <dd>{s.current_ctc}</dd>
            </div>
          )}
        </dl>
      )}

      {hasLinks && (
        <div className="pop-links">
          {hasValue(s.linkedin_url) && (
            <ExternalLink href={s.linkedin_url} className="btn btn-sm">
              LinkedIn ↗
            </ExternalLink>
          )}
          {hasValue(s.naukri_url) && (
            <ExternalLink href={s.naukri_url} className="btn btn-sm">
              Naukri ↗
            </ExternalLink>
          )}
          {s.resume && (
            <ResumeLink url={s.resume.url} filename={s.resume.filename} className="btn btn-sm">
              Resume ↗
            </ResumeLink>
          )}
        </div>
      )}

      {s.skills.length > 0 && (
        <ul className="chips chips-sm" aria-label="Skills">
          {s.skills.map((skill) => (
            <li key={skill} className="chip">
              {skill}
            </li>
          ))}
        </ul>
      )}

      {isEmptyProfile(profile) && <p className="pop-note">No profile details yet.</p>}

      <PopFooter onNavigate={onNavigate} />
    </>
  )
}

function PopFooter({ onNavigate }: { onNavigate: () => void }) {
  const { required, logout } = useAuth()
  return (
    <div className="pop-footer">
      <Link to="/profile" onClick={onNavigate}>
        Profile →
      </Link>
      {required && (
        <button type="button" className="btn btn-sm btn-ghost" onClick={logout}>
          Log out
        </button>
      )}
    </div>
  )
}

/**
 * Avatar button at the top right; opens a card with the main profile details, a link to the Profile
 * page and, when the app has a login, "Log out".
 */
export function ProfileMenu() {
  const profile = useProfile()
  const { open, setOpen, close, wrapRef, buttonRef, popRef, popId, onBlur } = usePopover()

  const name = profile.data?.summary.name ?? null
  const first = firstName(name)
  const failed = profile.data === undefined && !!profile.error

  const toggle = () => {
    if (open) {
      close(true)
      return
    }
    // The first load failed (e.g. backend was down): try again now that the user is looking.
    if (failed && !profile.loading) profile.reload()
    setOpen(true)
  }

  const buttonLabel = name ? `Profile: ${name}` : failed ? "Profile (couldn't load)" : 'Profile'

  return (
    <div className="profile-menu" ref={wrapRef} onBlur={onBlur}>
      <button
        ref={buttonRef}
        type="button"
        className="avatar-btn"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        aria-label={buttonLabel}
        title={buttonLabel}
        onClick={toggle}
      >
        <Avatar name={name} size="sm" />
        {first && <span className="avatar-btn-name">{first}</span>}
      </button>

      {open && (
        <div ref={popRef} id={popId} className="popover profile-pop" role="dialog" aria-label="Profile" tabIndex={-1}>
          {profile.data ? (
            <ProfileCard profile={profile.data} onNavigate={() => close(false)} />
          ) : profile.error ? (
            <>
              <p className="pop-note pop-note-error" role="alert">
                <strong>Couldn't load your profile.</strong> {errorMessage(profile.error)}
              </p>
              <div className="btn-row">
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => {
                    profile.reload()
                    popRef.current?.focus()
                  }}
                >
                  Retry
                </button>
              </div>
              <PopFooter onNavigate={() => close(false)} />
            </>
          ) : (
            <>
              <p className="pop-note" role="status">
                <span className="spinner" aria-hidden="true" /> Loading profile…
              </p>
              <PopFooter onNavigate={() => close(false)} />
            </>
          )}
        </div>
      )}
    </div>
  )
}
