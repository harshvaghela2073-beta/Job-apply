import { Link } from 'react-router-dom'
import { EmptyState } from '../components/DataState'

export function NotFoundPage() {
  return (
    <main className="page">
      <EmptyState title="Page not found.">
        <Link to="/">Go to the dashboard</Link>
      </EmptyState>
    </main>
  )
}
