import { Link, NavLink, Outlet } from 'react-router-dom'
import { useCounts } from '../hooks/useCounts'
import { CountBubble } from './Badge'
import { ConnectBanner } from './ConnectBanner'
import { ConnectionPills } from './Connections'
import { ProfileMenu } from './ProfileMenu'
import { ThemeToggle } from './ThemeToggle'
import { Wordmark } from './Wordmark'

/** The JOB·HUNT wordmark, linking to the Dashboard. */
function Brand() {
  return (
    <Link to="/" className="brand" aria-label="Job Hunt, Dashboard">
      <Wordmark />
    </Link>
  )
}

export function Layout() {
  const { summary } = useCounts()

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <Brand />
          <nav className="mainnav" aria-label="Main">
            <NavLink to="/" end>
              Dashboard
            </NavLink>
            <NavLink to="/linkedin">LinkedIn</NavLink>
            <NavLink to="/naukri">Naukri</NavLink>
            <NavLink to="/templates">
              Template Library <CountBubble count={summary.data?.unanswered_questions} label="unanswered questions" />
            </NavLink>
            <NavLink to="/analysis">Analysis</NavLink>
          </nav>
          <ConnectionPills />
          <ThemeToggle />
          <ProfileMenu />
        </div>
      </header>
      <ConnectBanner />
      <Outlet />
    </div>
  )
}
