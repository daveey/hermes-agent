/**
 * SESSION DOT STATE — one map from session id to the single status a surface
 * should paint, so the sidebar, the pane tabs, and the switcher can never
 * disagree about what a session is doing.
 *
 * It exists for two reasons the individual membership atoms cannot cover:
 *
 * 1. PRIORITY IN ONE PLACE. The signals overlap — a session can be working AND
 *    unread AND running a background job — and resolving that per call site is
 *    how surfaces drift apart.
 * 2. LINEAGE. Compression rotates a conversation's stored id, so a sidebar row,
 *    a persisted tile, and the route can each hold a different tip of one
 *    lineage. Every state is claimed under every alias (see `lineageAliases`),
 *    so a surface gets the right answer whichever tip it happens to hold.
 *
 * The inputs are all reference-stable across stream deltas, so this recomputes
 * on status edges rather than per token.
 *
 * Unread has TWO sources, both claiming the same state: the runtime marker
 * (a turn finished in the background while this window wasn't looking at it,
 * $unreadFinishedSessionIds — transient) and the backend's derived read-state
 * watermark (row.unread — persists across restarts and is visible to every
 * surface). The write side of the persisted flag lives in session-unread.ts.
 */

import { computed } from 'nanostores'

import { stableArray, stableRecord } from '@/lib/stable-array'
import type { SessionInfo } from '@/types/hermes'

import { $backgroundRunningSessionIds } from './composer-status'
import { $messagingSessions, $sessions, $unreadFinishedSessionIds, lineageAliases } from './session'
import {
  $attentionSessionIds,
  $draftSessionIds,
  $sessionStates,
  $stalledSessionIds,
  $workingSessionIds
} from './session-states'
import { $unreadWriteGuard, UNREAD_WRITE_GUARD_MS } from './session-unread-remote'
import { $subagentsBySession, activeSubagentCount } from './subagents'

// Sessions parked in async delegation: the parent turn has ended (busy=false —
// delegate_task(background=true) returns its handle the moment the children
// are spawned) while those subagents keep working for minutes. Without this
// input the sidebar row dropped to a plain idle dot mid-delegation, reading as
// "done" while work was still running in child sessions. Same runtime→stored
// bridge and fresh-chat fallback as $backgroundRunningSessionIds:
// $subagentsBySession is keyed by runtime id, surfaces key on stored ids, and
// lineageAliases covers whichever tip of the conversation a surface holds.
let delegatingIds: readonly string[] = []
export const $delegatingSessionIds = computed(
  [$subagentsBySession, $sessionStates, $sessions],
  (bySession, states, sessions) => {
    const ids = new Set<string>()

    for (const [runtimeId, items] of Object.entries(bySession)) {
      if (activeSubagentCount(items) === 0) {
        continue
      }

      for (const alias of lineageAliases(states[runtimeId]?.storedSessionId ?? runtimeId, sessions)) {
        ids.add(alias)
      }
    }

    return (delegatingIds = stableArray(delegatingIds, [...ids]))
  }
)

// [daveey] Sessions whose kanban tasks are still queued or running. Same story
// as async delegation, but the work lives in kanban worker processes for hours:
// `kanban_create` returns at once, the creating turn ends, and the sidebar read
// the session as done (green stripe) while its workers were still going. The
// backend stamps `kanban_active` on every list row (summed over the lineage).
let kanbanBusyIds: readonly string[] = []
export const $kanbanBusySessionIds = computed([$sessions, $messagingSessions], (sessions, messaging) => {
  const ids: string[] = []

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      if ((row.kanban_active ?? 0) > 0) {
        ids.push(row.id)
      }
    }
  }

  return (kanbanBusyIds = stableArray(kanbanBusyIds, ids))
})

// [daveey] Sessions owning a live background process, per the backend. The
// renderer's own $backgroundRunningSessionIds only knows sessions it polled
// after one of their tool events streamed into this window, so a turn the
// backend resumed after a restart (or a process another Hermes process
// registered) left the row green while e.g. `gh pr checks --watch` still ran.
// The backend stamps `background_active` (live pids, over the lineage).
let backgroundActiveIds: readonly string[] = []
export const $backgroundActiveSessionIds = computed([$sessions, $messagingSessions], (sessions, messaging) => {
  const ids: string[] = []

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      if ((row.background_active ?? 0) > 0) {
        ids.push(row.id)
      }
    }
  }

  return (backgroundActiveIds = stableArray(backgroundActiveIds, ids))
})

// [daveey] Sessions with a standing `/goal` still active. Between goal turns —
// especially while the judge has parked it on a `wait` timer — no turn is live,
// so the sidebar read the session as done (green stripe) although the goal loop
// will resume it on its own. The backend stamps `goal_active` on every list row
// (over the lineage); paused/done goals don't count.
let goalActiveIds: readonly string[] = []
export const $goalActiveSessionIds = computed([$sessions, $messagingSessions], (sessions, messaging) => {
  const ids: string[] = []

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      if (row.goal_active === true) {
        ids.push(row.id)
      }
    }
  }

  return (goalActiveIds = stableArray(goalActiveIds, ids))
})

export type SessionDotState = 'background' | 'draft' | 'idle' | 'needs-input' | 'stalled' | 'unread' | 'working'

/** The sidebar row's arc. A quiet turn is still authoritatively running, so
 *  `stalled` keeps it; a blocking prompt drops it, because the amber dot is the
 *  louder cue and two treatments at once fight each other. */
export const showsRunningArc = (state: SessionDotState): boolean => state === 'stalled' || state === 'working'

/** [daveey] The sidebar row's status stripe: `done` (green) when the agent
 *  finished its turn and is waiting on the user — read or not, so it stays
 *  after the row is opened; `stuck` (red) when an unfinished turn needs help —
 *  blocked on a prompt or gone quiet; `blocked` (grey) when the user parked
 *  it with ⌘B, which wins over everything. Working, background and drafts (no
 *  turn yet) get none — except that a session with kanban cards it created
 *  sitting `blocked` (waiting on a human) is `stuck` too unless a turn is
 *  actively working: it can't finish until someone unblocks them.
 *  `periodic` (loop glyph, grey) when the session owns a live cron job: its
 *  work recurs on its own, so it is never "done" — it counts as parked like
 *  `blocked` (⌘] / ⌘[ skip it); only a real prompt (needs-input) beats it. */
export type SessionStatusStripe = 'blocked' | 'done' | 'periodic' | 'stuck'

export const sessionStatusStripe = (
  state: SessionDotState,
  hasTurns: boolean,
  userBlocked = false,
  kanbanBlocked = 0,
  periodic = false
): null | SessionStatusStripe =>
  userBlocked
    ? 'blocked'
    : periodic && state !== 'needs-input'
    ? 'periodic'
    : state === 'needs-input' || state === 'stalled' || (kanbanBlocked > 0 && state !== 'working')
    ? 'stuck'
    : state === 'unread' || (state === 'idle' && hasTurns)
      ? 'done'
      : null

/** [daveey] Fold the row's OWN backend activity stamps into its resolved state.
 *  `$sessionDotStateById` only reads those stamps off `$sessions` /
 *  `$messagingSessions`, but a project-mode row older than the recents page
 *  comes from the project tree and is in neither — so a session with an active
 *  /goal, open kanban cards or a live background job painted done (green).
 *  Same claim (and priority) as the store: it beats idle/unread/draft only. */
export const withRowActivity = (
  state: SessionDotState,
  row: Pick<SessionInfo, 'background_active' | 'goal_active' | 'kanban_active'>
): SessionDotState =>
  (state === 'idle' || state === 'unread' || state === 'draft') &&
  (row.goal_active === true || (row.kanban_active ?? 0) > 0 || (row.background_active ?? 0) > 0)
    ? 'background'
    : state

/** Whether this turn is the session's own, live: brighter title, and the row's
 *  age yields to the actions menu. Wider than the arc — a turn waiting on an
 *  answer has not ended. */
export const hasLiveTurn = (state: SessionDotState): boolean => showsRunningArc(state) || state === 'needs-input'

/** The buckets the sidebar's status filter and ordering work in. `stalled` and
 *  `background` fold into the state a user would name them. */
export type SessionStatusBucket = 'draft' | 'idle' | 'needs-input' | 'unread' | 'working'

export const sessionStatusBucket = (state: SessionDotState = 'idle'): SessionStatusBucket =>
  state === 'stalled' || state === 'background' ? 'working' : state

const STATUS_RANK: Record<SessionStatusBucket, number> = {
  'needs-input': 0,
  working: 1,
  unread: 2,
  draft: 3,
  idle: 4
}

/** Loudest first — what ordering by status sorts on. */
export const sessionStatusRank = (state?: SessionDotState): number => STATUS_RANK[sessionStatusBucket(state)]

let dotStates: Readonly<Record<string, SessionDotState>> = {}

export const $sessionDotStateById = computed(
  [
    $attentionSessionIds,
    $workingSessionIds,
    $stalledSessionIds,
    $backgroundRunningSessionIds,
    $backgroundActiveSessionIds,
    $delegatingSessionIds,
    $kanbanBusySessionIds,
    $goalActiveSessionIds,
    $unreadFinishedSessionIds,
    $draftSessionIds,
    $sessions,
    $unreadWriteGuard
  ],
  (
    attention,
    working,
    stalled,
    background,
    backgroundActive,
    delegating,
    kanbanBusy,
    goalActive,
    unread,
    draft,
    sessions,
    unreadWriteGuard
  ) => {
    const next: Record<string, SessionDotState> = {}

    const claim = (ids: readonly string[], state: SessionDotState) => {
      for (const id of ids) {
        for (const alias of lineageAliases(id, sessions)) {
          next[alias] = state
        }
      }
    }

    // Weakest claim first — each pass overwrites the one above it, so the order
    // below IS the priority order. A blocking prompt outranks everything: it is
    // the only state that needs the user.
    //
    // Draft is weakest of all: it says only "no turn has happened here yet", so
    // the first thing that does happen speaks over it.
    claim(draft, 'draft')
    claim(unread, 'unread')

    // Persisted read state (backend watermark): a row marked unread keeps the
    // same emerald dot a background finish would paint, and survives
    // restarts. Same tier as the runtime marker — both mean "there is
    // something here you haven't opened". A list page that predates one of
    // our own writes is fenced out by the write guard: keep OUR value until a
    // page confirms it or the guard expires.
    const persistedUnread: string[] = []

    for (const s of sessions) {
      const entry = unreadWriteGuard.get(s.id)

      if (entry && Date.now() - entry.at < UNREAD_WRITE_GUARD_MS) {
        if (entry.value) {
          persistedUnread.push(s.id)
        }

        continue
      }

      if (s.unread === true) {
        persistedUnread.push(s.id)
      }
    }

    claim(persistedUnread, 'unread')

    claim(background, 'background')
    // Same claim from the backend's process registry (survives restarts).
    claim(backgroundActive, 'background')
    // Async delegation: the parent turn has ended but its subagents are still
    // running, so the session's work continues in child sessions. Same visual
    // claim as background processes — and it yields to `working` below the
    // moment the parent turn itself is live (synchronous orchestrator children).
    claim(delegating, 'background')
    // Kanban tasks it created are still queued or running: same claim.
    claim(kanbanBusy, 'background')
    // A standing goal will start its next turn by itself: same claim.
    claim(goalActive, 'background')
    claim(working, 'working')

    // Stalled REFINES working rather than rivalling it — the turn is still
    // authoritatively running, it has just gone quiet — so it only downgrades a
    // session already claimed as working. The hint outlives its turn by a tick
    // on some paths; without this it could invent a running session.
    for (const id of stalled) {
      for (const alias of lineageAliases(id, sessions)) {
        if (next[alias] === 'working') {
          next[alias] = 'stalled'
        }
      }
    }

    claim(attention, 'needs-input')

    return (dotStates = stableRecord(dotStates, next))
  }
)

/** Listed, non-archived rows whose resolved status is unread. Alias keys in
 *  `$sessionDotStateById` are ignored unless they are themselves a listed row. */
export function unreadSessionCount(
  byId: Readonly<Record<string, SessionDotState>>,
  ...lists: Array<readonly { archived?: boolean; id: string }[]>
): number {
  let n = 0

  for (const rows of lists) {
    for (const row of rows) {
      if (!row.archived && byId[row.id] === 'unread') {
        n++
      }
    }
  }

  return n
}

/** The titlebar badge. Cron sessions are deliberately EXCLUDED: cron runs
 *  finish unwatched by design, so counting them turns the badge into a cron
 *  run counter that is permanently lit (#93552). Their unread state stays
 *  visible where it belongs — the sidebar's cron section rows — and
 *  "mark all as read" still acks them (ackAllSessionsRead iterates cron rows). */
export const $unreadSessionCount = computed(
  [$sessionDotStateById, $sessions, $messagingSessions],
  (byId, sessions, messaging) => unreadSessionCount(byId, sessions, messaging)
)
