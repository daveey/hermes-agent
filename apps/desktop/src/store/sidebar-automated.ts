import type { SessionInfo } from '@/types/hermes'

import { $pinnedSessionIds, $sidebarShowAutomated } from './layout'
import { isSessionParked } from './session-blocked'
import { $sessionDotStateById, kanbanStripeCounts, type SessionDotState } from './session-dot-state'

/**
 * [daveey] Automated / test sessions in the sidebar.
 *
 * The backend stamps `automated: true` on rows whose source matches the owning
 * profile's `sessions.automated_sources` (`"*"` = the whole profile: bot
 * profiles, Slack followers, smoke tests). The sidebar drops those rows unless
 * "Show automated & test sessions" is on — except a row that still wants the
 * user: pinned, or painting a blocked / needs-attention (red or grey) stripe.
 *
 * ONE predicate: the sidebar list/tree filter and ⌘] / ⌘[ visible-order
 * stepping both call {@link sessionHiddenAsAutomated}, never a copy.
 */

type AutomatedRow = Pick<
  SessionInfo,
  | '_lineage_root_id'
  | 'automated'
  | 'cron_active'
  | 'id'
  | 'kanban_active'
  | 'kanban_attention'
  | 'kanban_active'
  | 'kanban_blocked'
  | 'kanban_held'
  | 'kanban_parked'
  | 'message_count'
  | 'pinned'
  | 'review_wait'
> &
  Partial<Pick<SessionInfo, 'execution_observations' | 'kanban_stalled' | 'kanban_external' | 'kanban_external_down' | 'kanban_observed_at' | 'muted'>>

export interface AutomatedRowContext {
  /** Explicit render clock for memoized filters; imperative callers use the current clock. */
  nowSeconds?: number
  /** Dot state for the row; defaults to `$sessionDotStateById`. */
  dotState?: SessionDotState
  /** Local pin ids; defaults to `$pinnedSessionIds`. */
  pinnedIds?: readonly string[]
  /** ⌘B parked flag; defaults to `isSessionParked(row.id, row)` (`row.muted`, else the local list). */
  userBlocked?: boolean
}

/** A bot row that is waiting on the user: needs input, or a red / grey stripe. */
export function automatedRowNeedsAttention(row: AutomatedRow, state: SessionDotState, userBlocked: boolean, nowSeconds?: number): boolean {
  if (state === 'needs-input' || state === 'stalled') {
    return true
  }

  const counts = kanbanStripeCounts(row, nowSeconds)

  // Visibility policy is not status/color authority. Missing protocol on a hidden bot must not
  // resurface every automated row; the surviving row still paints only its canonical status.
  return userBlocked || counts.blocked > 0 || counts.stalled > 0 || counts.externalDown > 0 ||
    row.review_wait === 'stuck' || (row.kanban_attention ?? 0) > 0
}

export function sessionHiddenAsAutomated(
  row: AutomatedRow,
  showAutomated: boolean = $sidebarShowAutomated.get(),
  ctx: AutomatedRowContext = {}
): boolean {
  if (showAutomated || row.automated !== true || row.pinned === true) {
    return false
  }

  const pins = ctx.pinnedIds ?? $pinnedSessionIds.get()

  if (pins.includes(row.id) || (row._lineage_root_id != null && pins.includes(row._lineage_root_id))) {
    return false
  }

  const state = ctx.dotState ?? $sessionDotStateById.get()[row.id] ?? 'idle'
  const userBlocked = ctx.userBlocked ?? isSessionParked(row.id, row)

  return !automatedRowNeedsAttention(row, state, userBlocked, ctx.nowSeconds)
}
