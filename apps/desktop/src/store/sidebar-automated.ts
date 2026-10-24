import type { SessionInfo } from '@/types/hermes'

import { $pinnedSessionIds, $sidebarShowAutomated } from './layout'
import { isSessionBlocked } from './session-blocked'
import { $sessionDotStateById, type SessionDotState, sessionStatusStripe } from './session-dot-state'

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
  | 'kanban_attention'
  | 'kanban_active'
  | 'kanban_blocked'
  | 'message_count'
  | 'pinned'
  | 'review_wait'
>

export interface AutomatedRowContext {
  /** Dot state for the row; defaults to `$sessionDotStateById`. */
  dotState?: SessionDotState
  /** Local pin ids; defaults to `$pinnedSessionIds`. */
  pinnedIds?: readonly string[]
  /** ⌘B parked flag; defaults to `isSessionBlocked(row.id)`. */
  userBlocked?: boolean
}

/** A bot row that is waiting on the user: needs input, or a red / grey stripe. */
export function automatedRowNeedsAttention(row: AutomatedRow, state: SessionDotState, userBlocked: boolean): boolean {
  if (state === 'needs-input' || state === 'stalled') {
    return true
  }

  const stripe = sessionStatusStripe(
    state,
    (row.message_count ?? 0) > 0,
    userBlocked,
    row.kanban_blocked ?? 0,
    row.cron_active === true,
    row.review_wait,
    row.kanban_attention ?? 0,
    row.kanban_active ?? 0
  )

  return stripe === 'stuck' || stripe === 'blocked' || stripe === 'attention'
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
  const userBlocked = ctx.userBlocked ?? isSessionBlocked(row.id)

  return !automatedRowNeedsAttention(row, state, userBlocked)
}
