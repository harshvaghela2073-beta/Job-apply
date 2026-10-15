import { createContext, useContext } from 'react'
import type { DashboardSummary } from '../api/types'
import type { QueryResult } from './useQuery'

export interface CountsValue {
  /** GET /api/dashboard/summary, shared by the nav badge and the Analysis page. */
  summary: QueryResult<DashboardSummary>
  /** Refetch the summary (call after any mutation that changes it). */
  refresh: () => void
}

export const CountsContext = createContext<CountsValue | null>(null)

export function useCounts(): CountsValue {
  const value = useContext(CountsContext)
  if (!value) throw new Error('useCounts must be used inside <CountsProvider>')
  return value
}
