import { NavLink, Outlet } from 'react-router-dom'
import type { Platform } from '../api/types'
import { PLATFORM_LABEL } from '../lib/format'

const TABS: Record<Platform, { to: string; label: string }[]> = {
  linkedin: [
    { to: 'easy-apply', label: 'Easy Apply' },
    { to: 'google-form', label: 'Google Form' },
    { to: 'company-website', label: 'Company Website' },
    { to: 'mail', label: 'Mail' },
  ],
  naukri: [
    { to: 'one-click', label: 'One-click' },
    { to: 'company-website', label: 'Company Website' },
  ],
}

/** Platform section: sub-tab bar plus the active sub-page. */
export function Section({ platform }: { platform: Platform }) {
  return (
    <>
      <nav className="subtabs" aria-label={`${PLATFORM_LABEL[platform]} sections`}>
        <div className="subtabs-inner">
          {TABS[platform].map((tab) => (
            <NavLink key={tab.to} to={tab.to}>
              {tab.label}
            </NavLink>
          ))}
        </div>
      </nav>
      <Outlet />
    </>
  )
}
