import { summarizeExecutionObservations } from '@/lib/execution-observations'
import type { SessionInfo } from '@/types/hermes'

/**
 * Sidebar consumer of the optional per-row `execution_observations` list (pure projector in
 * lib/execution-observations.ts; this module never re-implements its verdicts).
 *
 * Absent or empty list => `null`: the legacy kanban/turn/background stamps decide alone. Nothing here is certainty
 * invented for an older backend, and an empty list is NOT "zero work". Present entries are additive evidence:
 * they can only ADD live/waiting/needs-you/unknown signal on top of the legacy stamps, never erase one.
 * Expiry is time-only: the projector is re-run against the render clock, so a cached running/waiting/needs_user entry
 * degrades to unknown with no fetch (the existing 15 s `$kanbanObservationTick` re-evaluates; no new timer).
 */
export interface RowExecution {
  /** current running or queued work */
  live: number
  /** current waiting (dependency, schedule, review, ...) */
  waiting: number
  /** current needs_user */
  needsUser: number
  /** entries with no usable current evidence (expired, malformed, unverified, ...). Visible, never zero work. */
  unknown: number

  /** structured human requests still outstanding, including ones retained by expired entries */
  outstanding: number
}

type Stamped = Pick<SessionInfo, 'execution_observations'>

export function rowExecution(row: Partial<Stamped> | undefined, nowSeconds: number = Date.now() / 1000): null | RowExecution {
  const list = row?.execution_observations

  if (!Array.isArray(list) || list.length === 0) {
    return null
  }

  // Capacity is diagnostic context, not another work item. The existing capacity producer stamps
  // kanban.dispatch; kanban.capacity is its explicit diagnostic identity. Never filter by state/reason:
  // malformed or unknown observations from any other source must remain visible.
  const work = list.filter(entry => {
    const source = entry !== null && typeof entry === 'object' ? (entry as Record<string, unknown>).source : undefined

    return source !== 'kanban.dispatch' && source !== 'kanban.capacity'
  })

  if (work.length === 0) {
    return null
  }

  const summary = summarizeExecutionObservations(work, nowSeconds, 'sidebar')

  const { counts } = summary

  return {
    live: counts.running + counts.queued,
    needsUser: counts.needs_user,
    outstanding: summary.outstanding_human_requests,
    unknown: counts.unknown,
    waiting: counts.waiting
  }
}

/** Evidence that keeps a row off done: live/waiting/needs-you work, an outstanding ask, or unresolved uncertainty. */
export const executionHoldsRow = (e: null | RowExecution): boolean =>
  e !== null && e.live + e.waiting + e.needsUser + e.unknown + e.outstanding > 0

/** Stable signature for change detection (row signatures and the published per-row map). */
export const executionKey = (e: null | RowExecution): string =>
  e ? `${e.live}/${e.waiting}/${e.needsUser}/${e.unknown}/${e.outstanding}` : ''
