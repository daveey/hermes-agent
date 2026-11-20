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

import { summarizeExecutionObservations } from '@/lib/execution-observations'
import { kanbanExternalStripeCounts } from '@/lib/external-run'
import { outcomeStripe } from '@/lib/session-outcome'
import { stableArray, stableRecord } from '@/lib/stable-array'
import type { SessionBackgroundProcess, SessionInfo } from '@/types/hermes'
import type { SessionOutcome } from '@/types/session-outcome'

import { $backgroundRunningSessionIds, $backgroundStatusBySession } from './composer-status'
import { executionHoldsRow, executionKey, rowExecution } from './execution-observation-row'
import { $kanbanObservationTick, hasFreshKanbanObservation } from './kanban-observation'
import { $liveWorkRoster, noteEventFed, rosterLiveIds, rosterSaysIdle, selectWorkObservations } from './live-work-roster'
import { $pausedQueueSessionIds } from './owner-queue-reconcile'
import { $messagingSessions, $sessions, $unreadFinishedSessionIds, lineageAliases } from './session'
import {
  $attentionSessionIds,
  $draftSessionIds,
  $sessionStates,
  $stalledSessionIds,
  $workingSessionIds
} from './session-states'
import { $unreadWriteGuard, UNREAD_WRITE_GUARD_MS } from './session-unread-remote'
import { $subagentsBySession } from './subagents'

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
  [$subagentsBySession, $sessionStates, $sessions, $liveWorkRoster, $kanbanObservationTick],
  (bySession, states, sessions, roster) => {
    const ids = new Set<string>()
    const eventFedAt = new Map<string, number>()

    for (const [runtimeId, items] of Object.entries(bySession)) {
      if (items.length === 0) {
        continue
      }

      const aliases = lineageAliases(states[runtimeId]?.storedSessionId ?? runtimeId, sessions)
      const heardAt = Math.max(...items.map(item => item.updatedAt || 0))
      noteEventFed(eventFedAt, aliases, heardAt)

      const evidence = items.filter(i => i.status === 'running').flatMap(i => i.executionObservations ?? [])

      if (summarizeExecutionObservations(selectWorkObservations(evidence, Date.now() / 1000), Date.now() / 1000, 'sidebar').counts.running > 0 && !rosterSaysIdle(roster, 'subagents', aliases, heardAt)) {
        for (const alias of aliases) {
          ids.add(alias)
        }
      }
    }

    // Rows no current event feed speaks for (off screen, every row after a reload).
    for (const alias of rosterLiveIds(roster, 'subagents', eventFedAt, sessions)) {
      ids.add(alias)
    }

    return (delegatingIds = stableArray(delegatingIds, [...ids]))
  }
)

// [daveey] Sessions whose kanban tasks are still queued, running or blocked. Same story
// as async delegation, but the work lives in kanban worker processes for hours:
// `kanban_create` returns at once, the creating turn ends, and the sidebar read
// the session as done (green stripe) while its workers were still going. The
// backend stamps `kanban_active` on every list row (summed over the lineage).
let kanbanBusyIds: readonly string[] = []
export const $kanbanBusySessionIds = computed([$sessions, $messagingSessions, $kanbanObservationTick, $liveWorkRoster, $subagentsBySession], (sessions, messaging) => {
  const ids: string[] = []

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      // [daveey] `kanban_unknown`: the backend could not read the session's cards (local boards or another
      // gateway's replica). Unknown is not "no work", so the row stays off done until a read succeeds.
      if (
        (row.kanban_active ?? 0) > 0 ||
        (row.kanban_blocked ?? 0) > 0 ||
        row.kanban_unknown === true ||
        (row.kanban_remote_stale ?? 0) > 0 ||
        !hasFreshKanbanObservation(row) ||
        executionHoldsRow(rowExecution(row))
      ) {
        ids.push(row.id)
      }
    }
  }

  return (kanbanBusyIds = stableArray(kanbanBusyIds, ids))
})

/** Per-row execution evidence signature at the render clock. Re-evaluated by the existing 15 s tick (time-only
 *  expiry, no fetch); the reference changes only when some row's verdict does, so subscribers re-render on
 *  transitions, not on every tick. Rows without observations are absent. */
let executionKeys: Readonly<Record<string, string>> = {}
export const $executionRowKeys = computed([$sessions, $messagingSessions, $kanbanObservationTick, $liveWorkRoster, $backgroundStatusBySession, $subagentsBySession], (sessions, messaging) => {
  const next: Record<string, string> = {}

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      const key = executionKey(rowExecution(row))

      if (key) {
        next[row.id] = key
      }
    }
  }

  return (executionKeys = stableRecord(executionKeys, next))
})

// [daveey] Sessions whose kanban cards finished while no backend held the chat:
// the result is waiting in the kanban store (`kanban_unseen`, until the
// unloaded-session sweeper hands it over; after that it is a persisted unread
// row). It is "done, results waiting" — the same green as an unread finish —
// and yields to running cards, which keep the row in `background`.
let kanbanUnseenIds: readonly string[] = []
export const $kanbanUnseenSessionIds = computed([$sessions, $messagingSessions], (sessions, messaging) => {
  const ids: string[] = []

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      if ((row.kanban_unseen ?? 0) > 0) {
        ids.push(row.id)
      }
    }
  }

  return (kanbanUnseenIds = stableArray(kanbanUnseenIds, ids))
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

// [daveey] WHAT keeps a row off green when it is a background process: the
// backend lists each live process (`background_processes`) next to the count.
// The sidebar row shows a process badge from this — a finished chat held only
// by a 72 h watcher used to sit un-green with nothing on the row to say why.
const NO_PROCESSES: readonly SessionBackgroundProcess[] = []

/** The live background processes a row lists, oldest first. Falls back to
 *  placeholder entries when an older backend stamped only the count. */
export const rowBackgroundProcesses = (
  row: Pick<SessionInfo, 'background_active' | 'background_processes'>
): readonly SessionBackgroundProcess[] => {
  if (row.background_processes?.length) {
    return row.background_processes
  }

  const n = row.background_active ?? 0

  return n > 0 ? Array.from({ length: n }, (_, i) => ({ command: '', id: `#${i}` })) : NO_PROCESSES
}

let backgroundProcessesById: Readonly<Record<string, readonly SessionBackgroundProcess[]>> = {}
export const $backgroundProcessesBySessionId = computed([$sessions, $messagingSessions], (sessions, messaging) => {
  const next: Record<string, readonly SessionBackgroundProcess[]> = {}

  for (const rows of [sessions, messaging]) {
    for (const row of rows) {
      const procs = rowBackgroundProcesses(row)

      if (procs.length > 0) {
        next[row.id] = procs
      }
    }
  }

  return (backgroundProcessesById = stableRecord(backgroundProcessesById, next))
})

/** Drop a stopped process from a row at once (the next list poll confirms):
 *  the badge loses it, and with nothing else holding it the row goes green. */
export const withoutBackgroundProcess = (row: SessionInfo, processId: string): SessionInfo => {
  const listed = row.background_processes ?? []

  if (!listed.some(proc => proc.id === processId)) {
    return row
  }

  const left = listed.filter(proc => proc.id !== processId)
  const active = Math.max(0, (row.background_active ?? listed.length) - 1)
  const { background_active: _a, background_processes: _p, ...rest } = row

  return {
    ...rest,
    ...(active > 0 ? { background_active: active } : {}),
    ...(left.length > 0 ? { background_processes: left } : {})
  }
}

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
 *  `stalled` keeps it; so does `background`, because a turn that ended on
 *  delegate_task(background=true) or terminal(background=true) still has work
 *  running in the session, and a row with no arc read as finished. A blocking
 *  prompt drops it, because the amber dot is the louder cue and two treatments
 *  at once fight each other. The dot still tells background apart (hollow). */
export const showsRunningArc = (state: SessionDotState): boolean =>
  state === 'background' || state === 'stalled' || state === 'working'

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
//  [daveey] `attention` (amber) replaces `done` when a kanban card this session
//  made finished with a handoff that admits dropped / deferred / unrun work and
//  no follow-up card (backend `kanban_attention`): it looks done but isn't.
//  [daveey] `waiting` (grey, card glyph) when the chat handed its work to
//  kanban cards that are still open (`kanban_active`): it is not done and not
//  stuck, it waits on them — never green, never blank. Parked like `periodic`
//  (⌘] / ⌘[ skip it); a blocked card (red) and a live turn beat it.
//  [daveey] `stalled` (amber, card glyph) replaces `waiting` when one of those parked cards waits on something only an
//  agent can end and has for over 2 h (backend `kanban_stalled`): nothing is producing what it waits for. Still not
//  the user's move (red stays for that), and parked like `waiting` for ⌘] / ⌘[.
export type SessionStatusStripe =
  | 'attention'
  | 'blocked'
  | 'done'
  | 'external'
  | 'external_down'
  | 'periodic'
  | 'stalled'
  | 'stuck'
  | 'waiting'

/** [daveey] Backend `review_wait` stamp: the session's latest reply links an
 *  open PR still needing a code review. `stuck` = the review is on the user
 *  (requested from them, nobody requested, or others asked over 24h ago) →
 *  red; `parked` = only others requested, under 24h → grey like ⌘B. Either
 *  beats a running watcher (`background`) and `done`, never a working turn. */
export type ReviewWait = 'parked' | 'stuck'

/** [daveey] Red: an unfinished turn needs help, or (unless a turn is working) its cards or a review wait on the user. */
const isStuck = (state: SessionDotState, kanbanBlocked: number, reviewWait: null | ReviewWait | undefined): boolean =>
  state === 'needs-input' ||
  state === 'stalled' ||
  (state !== 'working' && (kanbanBlocked > 0 || reviewWait === 'stuck'))

/** [daveey] The parked / waiting stripes a non-working row can wear before it counts as finished. */
const parkedStripe = (
  state: SessionDotState,
  reviewWait: null | ReviewWait | undefined,
  kanbanWaiting: number,
  kanbanStalled: number,
  kanbanExternal = 0
): null | SessionStatusStripe => {
  if (state === 'working') {
    return null
  }

  if (reviewWait === 'parked') {
    return 'blocked'
  }

  // [daveey] Light-blue external run beats grey waiting / amber stalled.
  if (kanbanExternal > 0) {
    return 'external'
  }

  if (kanbanWaiting > 0) {
    return kanbanStalled > 0 ? 'stalled' : 'waiting'
  }

  return null
}

/** [daveey] A finished turn: green, or amber when a card it made admits dropped work. */
const settledStripe = (state: SessionDotState, hasTurns: boolean, kanbanAttention: number): null | SessionStatusStripe =>
  state === 'unread' || (state === 'idle' && hasTurns) ? (kanbanAttention > 0 ? 'attention' : 'done') : null

const derivedStatusStripe = (
  state: SessionDotState,
  hasTurns: boolean,
  userBlocked: boolean,
  kanbanBlocked: number,
  periodic: boolean,
  reviewWait: null | ReviewWait | undefined,
  kanbanAttention: number,
  kanbanWaiting: number,
  kanbanStalled: number,
  kanbanExternal = 0,
  kanbanExternalDown = 0
): null | SessionStatusStripe => {
  if (userBlocked) {
    return 'blocked'
  }

  if (periodic && state !== 'needs-input') {
    return 'periodic'
  }

  if (isStuck(state, kanbanBlocked, reviewWait)) {
    return 'stuck'
  }

  // [daveey] external_down is red at the stuck tier (after a live turn / ⌘B / human ask).
  if (state !== 'working' && kanbanExternalDown > 0) {
    return 'external_down'
  }

  return (
    parkedStripe(state, reviewWait, kanbanWaiting ?? 0, kanbanStalled ?? 0, kanbanExternal) ??
    settledStripe(state, hasTurns, kanbanAttention ?? 0)
  )
}

/** [daveey] The invariant's test, shared by the stripe and the violation report. */
export const hasOpenHumanAsk = (
  userBlocked: boolean,
  kanbanBlocked: number,
  reviewWait: null | ReviewWait | undefined
): boolean => !userBlocked && (kanbanBlocked > 0 || reviewWait === 'stuck')

/** [daveey] Whether a stored outcome may paint the stripe: a ⌘B park or the chat's own live turn beats it, but work
 *  that outlives the turn (a running card, a background job) does not. The stripe's tooltip must use this same test,
 *  or a red decision stripe on a chat whose card is running shows no question. */
export const outcomePaintsStripe = (state: SessionDotState, userBlocked: boolean): boolean =>
  !userBlocked && !hasOwnLiveTurn(state)

//  [daveey] `outcome`: the row's stored outcome (session-outcome contract). When present it wins over every derived
//  input below; a ⌘B park and a live turn (working / quiet / needs-input) still beat it. Absent (an older backend)
//  → the derivation below, unchanged.
export const sessionStatusStripe = (
  state: SessionDotState,
  hasTurns: boolean,
  userBlocked = false,
  kanbanBlocked = 0,
  periodic = false,
  reviewWait: null | ReviewWait | undefined = null,
  kanbanAttention = 0,
  kanbanWaiting = 0,
  kanbanStalled = 0,
  outcome: null | SessionOutcome | undefined = null,
  sourceUnavailable = false,
  kanbanExternal = 0,
  kanbanExternalDown = 0
): null | SessionStatusStripe => {
  // [daveey] The one invariant: an open human ask (blocked/held cards, `needs_you`, an outstanding execution ask, a
  // stuck review) is red unless the user ⌘B-parked the row. Outcome, periodic and working only choose among the
  // non-red stripes below (the badge and the stripe once disagreed through each of those precedence edges).
  if (hasOpenHumanAsk(userBlocked, kanbanBlocked, reviewWait)) {
    return 'stuck'
  }

  if (outcomePaintsStripe(state, userBlocked)) {
    const fromOutcome = outcomeStripe(outcome)
    // Completion is historical intent, not permission to hide current unfinished/unknown work.
    // Decisions and unresolved outcomes retain their stronger attention semantics.
    const completionHeld = sourceUnavailable || state === 'background' || kanbanBlocked > 0 || kanbanWaiting > 0

    if (fromOutcome && (fromOutcome !== 'done' || !completionHeld)) {
      return fromOutcome
    }
  }

  const derived = derivedStatusStripe(
    state,
    hasTurns,
    userBlocked,
    kanbanBlocked,
    periodic,
    reviewWait,
    kanbanAttention,
    kanbanWaiting,
    kanbanStalled,
    kanbanExternal,
    kanbanExternalDown
  )

  if (sourceUnavailable && !userBlocked && !hasOwnLiveTurn(state) && hasTurns && (derived === null || derived === 'done')) {
    return 'waiting'
  }

  return derived
}

type KanbanCounts = Pick<SessionInfo, 'kanban_active' | 'kanban_blocked' | 'kanban_held' | 'kanban_parked'> &
  Partial<
    Pick<
      SessionInfo,
      | 'execution_observations'
      | 'kanban_external'
      | 'kanban_external_cards'
      | 'kanban_external_down'
      | 'kanban_external_unknown'
      | 'kanban_stalled'
      | 'needs_you'
    >
  >

/** [daveey] Open kanban cards someone is actually working (running/ready/review, or a todo about to be promoted).
 *  `kanban_parked` (scheduled, non-human blocks, todo behind an open parent) and `kanban_held` (todo behind a human
 *  ask upstream) are subsets of `kanban_active`. A backend that predates them stamps no `kanban_parked`: then every
 *  open card counts as live, as before. */
export const liveKanbanCount = (row: KanbanCounts): number =>
  row.kanban_parked === undefined
    ? (row.kanban_active ?? 0)
    : Math.max(0, (row.kanban_active ?? 0) - row.kanban_parked - (row.kanban_held ?? 0))

/** [daveey] The kanban inputs of `sessionStatusStripe`: `blocked` = cards blocked on a human plus queued cards held
 *  behind one (red); `waiting` = parked cards when nothing is live (grey). Live cards give neither, so a chat whose
 *  cards are running reads as working (no stripe) instead of grey like one parked until Thursday. Against a
 *  backend without `kanban_parked` every open card waits, as before. */
export const kanbanStripeCounts = (
  row: KanbanCounts,
  nowSeconds: number = Date.now() / 1000
): {
  blocked: number
  external: number
  externalDown: number
  externalUnknown: number
  stalled: number
  waiting: number
} => {
  // Optional execution evidence only adds: asks (even retained by an expired entry) are red, current waits are grey,
  // current running/queued work defers the grey wait exactly like a live card does.
  const exec = rowExecution(row, nowSeconds)
  // `needs_you` (one backend predicate, same snapshot as the counts) is authoritative when the backend stamps it;
  // an older backend falls back to the blocked-card count.
  const asks = row.needs_you === undefined ? (row.kanban_blocked ?? 0) : row.needs_you.length
  const blocked = asks + (row.kanban_held ?? 0) + (exec ? Math.max(exec.needsUser, exec.outstanding) : 0)
  const execLive = exec?.live ?? 0
  const execWaiting = exec?.waiting ?? 0
  const ext = kanbanExternalStripeCounts(row, nowSeconds)

  if (row.kanban_parked === undefined) {
    return {
      blocked,
      external: ext.external,
      externalDown: ext.down,
      externalUnknown: ext.unknown,
      stalled: 0,
      waiting: execLive > 0 ? 0 : (row.kanban_active ?? 0) + execWaiting
    }
  }

  const waiting = liveKanbanCount(row) + execLive > 0 ? 0 : row.kanban_parked + execWaiting

  // [daveey] Stalled is a subset of parked: only meaningful while the row is waiting at all.
  return {
    blocked,
    external: ext.external,
    externalDown: ext.down,
    externalUnknown: ext.unknown,
    stalled: waiting > 0 ? Math.min(row.kanban_stalled ?? 0, waiting) : 0,
    waiting
  }
}

/** [daveey] Fold the row's OWN backend activity stamps into its resolved state.
 *  `$sessionDotStateById` only reads those stamps off `$sessions` /
 *  `$messagingSessions`, but a project-mode row older than the recents page
 *  comes from the project tree and is in neither — so a session with an active
 *  /goal, open kanban cards or a live background job painted done (green).
 *  Same claim (and priority) as the store: it beats idle/unread/draft only. */
type RowActivity = Pick<SessionInfo, 'background_active' | 'goal_active' | 'kanban_active' | 'kanban_blocked'> &
  Partial<Pick<SessionInfo, 'kanban_held' | 'kanban_parked'>> &
  Partial<
    Pick<
      SessionInfo,
      | 'execution_observations'
      | 'kanban_external'
      | 'kanban_external_cards'
      | 'kanban_external_down'
      | 'kanban_external_unknown'
      | 'kanban_observed_at'
      | 'kanban_remote_stale'
      | 'kanban_unknown'
      | 'kanban_unseen'
      | 'offline_cached'
    >
  > &
  Partial<Pick<SessionInfo, 'message_count' | 'turn_live'>>

/** [daveey] Work the row's own stamps say is still in flight outside a turn: a goal, cards, a background job. */
const rowHasBackgroundWork = (row: RowActivity, nowSeconds: number = Date.now() / 1000): boolean => {
  const ext = kanbanExternalStripeCounts(row, nowSeconds)

  return (
    row.goal_active === true ||
    liveKanbanCount(row) > 0 ||
    row.kanban_unknown === true ||
    (row.kanban_remote_stale ?? 0) > 0 ||
    !hasFreshKanbanObservation(row) ||
    executionHoldsRow(rowExecution(row)) ||
    (row.kanban_blocked ?? 0) > 0 ||
    (row.background_active ?? 0) > 0 ||
    ext.external > 0 ||
    ext.down > 0
  )
}

/** [daveey] What a `background` dot's label may claim about its work. */
export type BackgroundDotLabel = 'external' | 'external_down' | 'running' | 'unknown' | 'waiting'

/** [daveey] The words on a `background` dot. The claim itself stays (unknown or parked work must never read as done);
 *  only the label follows the evidence:
 *  - `running` when something positively runs: a process (renderer-polled or backend `background_active`), async
 *    subagents, a goal, or current running/queued execution. `liveElsewhere` is the store's own running evidence for
 *    the row (a renderer-polled process, async subagents).
 *  - `unknown` when nothing runs and the claim rests on unknown or unfresh evidence: a failed or stale kanban read, an
 *    expired/absent observation, or work whose execution evidence is unknown.
 *  - `waiting` when the only open work is known waiting on a fresh read: parked cards or current waiting
 *    observations, with no unparked card, blocked or held card, needs-you entry or outstanding ask.
 *  Anything else (blocked, held or needs-you work) keeps the existing `running` label. */
export const backgroundDotLabel = (
  row: RowActivity,
  liveElsewhere = false,
  nowSeconds: number = Date.now() / 1000
): BackgroundDotLabel => {
  if (liveElsewhere || row.goal_active === true || (row.background_active ?? 0) > 0) {
    return 'running'
  }

  const exec = rowExecution(row, nowSeconds)

  if ((exec?.live ?? 0) > 0) {
    return 'running'
  }

  const ext = kanbanExternalStripeCounts(row, nowSeconds)

  if (ext.down > 0) {
    return 'external_down'
  }

  if (ext.external > 0) {
    return 'external'
  }

  if (rowEvidenceUnknown(row, exec)) {
    return 'unknown'
  }

  return rowOnlyWaiting(row, exec) ? 'waiting' : 'running'
}

/** [daveey] The claim rests on unknown or unfresh evidence: a failed or stale kanban read, an expired/absent
 *  observation, or work whose execution evidence is unknown. */
const rowEvidenceUnknown = (row: RowActivity, exec: ReturnType<typeof rowExecution>): boolean =>
  row.kanban_unknown === true ||
  (row.kanban_remote_stale ?? 0) > 0 ||
  !hasFreshKanbanObservation(row) ||
  (exec?.unknown ?? 0) > 0

/** [daveey] The only open work is known waiting: parked cards or current waiting observations, and no unparked card,
 *  blocked card, held card, needs-you entry or outstanding ask. A held card (todo behind a human ask) is red in
 *  `kanbanStripeCounts`, so it keeps the `running` label like a blocked one instead of reading "waiting". */
const rowOnlyWaiting = (row: RowActivity, exec: ReturnType<typeof rowExecution>): boolean => {
  const parked = row.kanban_parked ?? 0

  return (
    parked + (exec?.waiting ?? 0) > 0 &&
    liveKanbanCount(row) === 0 &&
    (row.kanban_blocked ?? 0) === 0 &&
    (row.kanban_held ?? 0) === 0 &&
    (exec?.needsUser ?? 0) === 0 &&
    (exec?.outstanding ?? 0) === 0
  )
}

export const withRowActivity = (state: SessionDotState, row: RowActivity): SessionDotState => {
  if (row.offline_cached) {
    return 'idle'
  }

  // [daveey] A turn the backend reports running (any process, any gateway) is a live turn even when this window
  // never saw its stream: a backend-woken close-out turn in an unopened chat, or a chat on a gateway other than the
  // active one (only the active gateway's `session.active_list` is polled). It beats every non-live state, so the
  // stored outcome (a red decision) yields to it like any live turn.
  if (row.turn_live === true && !hasOwnLiveTurn(state)) {
    return 'working'
  }

  // [daveey] The row knows its own history: a store-side `draft` claim (a client state with no messages, matched
  // against `$sessions` only) is wrong for a row that has messages. A project-tree row older than the recents page is
  // not in `$sessions`, so a cold state for it (transcript released) painted "nothing sent yet" and no stripe.
  const settled = state === 'draft' && (row.message_count ?? 0) > 0 ? 'idle' : state

  if ((settled === 'idle' || settled === 'unread' || settled === 'draft') && rowHasBackgroundWork(row)) {
    return 'background'
  }

  return (settled === 'idle' || settled === 'draft') && (row.kanban_unseen ?? 0) > 0 ? 'unread' : settled
}

/** [daveey] Background work keeps the session live, but is not its own turn:
 *  it must not suppress stored outcomes or outrank a backend turn_live stamp. */
const hasOwnLiveTurn = (state: SessionDotState): boolean =>
  state === 'working' || state === 'stalled' || state === 'needs-input'

/** Whether the session is live, including work that outlives its own turn. */
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
    $kanbanUnseenSessionIds,
    $goalActiveSessionIds,
    $unreadFinishedSessionIds,
    $draftSessionIds,
    $sessions,
    $unreadWriteGuard,
    $pausedQueueSessionIds
  ],
  (
    attention,
    working,
    stalled,
    background,
    backgroundActive,
    delegating,
    kanbanBusy,
    kanbanUnseen,
    goalActive,
    unread,
    draft,
    sessions,
    unreadWriteGuard,
    paused
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
    // Finished kanban cards whose result no chat has been handed yet: green,
    // below every live claim (running cards, a goal, a turn) painted next.
    claim(kanbanUnseen, 'unread')

    claim(background, 'background')
    // Same claim from the backend's process registry (survives restarts).
    claim(backgroundActive, 'background')
    // Async delegation: the parent turn has ended but its subagents are still
    // running, so the session's work continues in child sessions. Same visual
    // claim as background processes — and it yields to `working` below the
    // moment the parent turn itself is live (synchronous orchestrator children).
    claim(delegating, 'background')
    // Blocked cards are unfinished too, even after a goal completes. The
    // row's stripe adds the red treatment; its dot must not claim finished.
    claim(kanbanBusy, 'background')
    // A standing goal will start its next turn by itself: same claim.
    claim(goalActive, 'background')
    // A queue paused behind a turn lost in a restart waits on the user's Discard, and a turn that
    // never finished must not read as finished-unread. A turn actually running speaks over it.
    claim(paused, 'needs-input')
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

// [daveey] Stripe-transition trace. Rows keep getting reported painted `done` (green) while their work is still
// queued, and every time someone looks the row is right again, so no bad payload was ever captured. This writes one
// desktop.log line (`stripe-transition <sid> …`) whenever a session's stripe changes, carrying the raw row fields that
// fed it and where the row came from. Change-only and capped per session per minute, so a steady sidebar costs a Map
// lookup per row render and a flapping row can't flood the log.

/** Where the sidebar's `$sessions` page last came from: `sessions.changed` (backend change event), `list-refetch`
 *  (any other refresh: boot, turn end, reconnect, filter), or `initial` before the first page. Rows not in
 *  `$sessions` come from the project tree; `offline_cached` rows from the offline cache. */
let sessionListSource = 'initial'
let pendingRefreshReason: null | string = null

/** Tag the NEXT sidebar list refresh with why it ran (e.g. `sessions.changed`). */
export const markSessionListRefreshReason = (reason: string): void => {
  pendingRefreshReason = reason
}

/** Consume the pending refresh reason at the start of a refresh (default `list-refetch`). */
export const takeSessionListRefreshReason = (): string => {
  const reason = pendingRefreshReason ?? 'list-refetch'
  pendingRefreshReason = null

  return reason
}

/** Record the source of the page just written into `$sessions`. */
export const noteSessionListSource = (source: string): void => {
  sessionListSource = source
}

export const STRIPE_TRANSITION_LOG_CAP_PER_MIN = 6
const STRIPE_TRANSITION_WINDOW_MS = 60_000

interface StripeTraceEntry {
  stripe: null | SessionStatusStripe
  /** The red reasons last logged (`stuckReasons` joined); empty unless `stuck`. */
  reasonsKey: string
  emitted: number[]
  suppressed: number
}

const stripeTrace = new Map<string, StripeTraceEntry>()

/** Test hook: forget every remembered stripe. */
export const resetStripeTransitionLog = (): void => {
  stripeTrace.clear()
  sessionListSource = 'initial'
  pendingRefreshReason = null
}

export type StripeTraceRow = Pick<SessionInfo, 'id'> &
  Partial<
    Pick<
      SessionInfo,
      | 'archived'
      | 'background_active'
      | 'connection_id'
      | 'cron_active'
      | 'execution_observations'
      | 'goal_active'
      | 'kanban_active'
      | 'kanban_blocked'
      | 'kanban_blocked_on'
      | 'kanban_held'
      | 'kanban_observed_at'
      | 'kanban_parked'
      | 'kanban_stalled'
      | 'kanban_waiting_on'
      | 'muted'
      | 'needs_you'
      | 'offline_cached'
      | 'outcome'
      | 'offline_synced_at'
      | 'profile'
      | 'review_wait'
      | 'review_wait_pr'
    >
  >

const traceValue = (value: unknown): string =>
  value === undefined ? '-' : Array.isArray(value) ? `[${value.join(',')}]` : String(value)

function rowListSource(row: StripeTraceRow): string {
  if (row.offline_cached) {
    return 'cache'
  }

  const conn = row.connection_id ?? ''

  if ($sessions.get().some(s => s.id === row.id && (s.connection_id ?? '') === conn)) {
    return sessionListSource
  }

  // [daveey] A hub-replica / boot-snapshot fallback row (carries no activity stamps), not a projects-tree row.
  return row.offline_synced_at !== undefined ? 'fallback' : 'project-tree'
}

/** [daveey] WHY a row's stripe is red (`stuck`), one entry per input that holds, in the same terms as
 *  `sessionStatusStripe`: `needs-input:prompt runtime=<ids>` (a blocking prompt on those runtimes),
 *  `needs-input:paused-queue`, `stalled` (a quiet turn), `kanban-blocked:<n> on=<cards>` (+ `held=` / `asks=`),
 *  `review:<pr>`, `outcome:decision`. Agents could not see the renderer-only half of this and guessed
 *  (selfheal desktop-54ae373894), so every red edge is logged and reported to the session's gateway with it. */
export function stuckReasons(
  row: StripeTraceRow,
  dotState: SessionDotState,
  nowSeconds: number = Date.now() / 1000
): string[] {
  const reasons: string[] = []

  if (dotState === 'needs-input') {
    const aliases = new Set(lineageAliases(row.id, $sessions.get()))
    aliases.add(row.id)

    const runtimes = Object.entries($sessionStates.get())
      .filter(([runtimeId, s]) => s?.needsInput && aliases.has(s.storedSessionId ?? runtimeId))
      .map(([runtimeId]) => runtimeId)

    const paused = $pausedQueueSessionIds.get().some(id => aliases.has(id))

    if (runtimes.length > 0) {
      reasons.push(`needs-input:prompt runtime=${runtimes.join(',')}`)
    }

    if (paused) {
      reasons.push('needs-input:paused-queue')
    }

    if (!runtimes.length && !paused) {
      reasons.push('needs-input:unknown')
    }
  }

  if (dotState === 'stalled') {
    reasons.push('stalled')
  }

  // [daveey] Open asks are red whatever the dot says (a working turn no longer hides them), so their reasons are
  // always listed.
  const counts = kanbanStripeCounts(row, nowSeconds)

  if (counts.blocked > 0) {
    const exec = rowExecution(row, nowSeconds)
    const parts = [`kanban-blocked:${counts.blocked}`]

    if (row.kanban_blocked_on?.length) {
      parts.push(`on=${row.kanban_blocked_on.join(',')}`)
    }

    if ((row.kanban_held ?? 0) > 0) {
      parts.push(`held=${row.kanban_held}`)
    }

    if (exec && Math.max(exec.needsUser, exec.outstanding) > 0) {
      parts.push(`asks=${Math.max(exec.needsUser, exec.outstanding)}`)
    }

    reasons.push(parts.join(' '))
  }

  if (row.review_wait === 'stuck') {
    reasons.push(`review:${row.review_wait_pr ?? '?'}`)
  }

  if (!hasOwnLiveTurn(dotState) && outcomeStripe(row.outcome) === 'stuck') {
    reasons.push(`outcome:${row.outcome?.kind}`)
  }

  return reasons
}

/** [daveey] The inputs `stuckReasons` reads for a `needs-input` row that no row prop carries: which runtimes hold a
 *  prompt (for which stored session) and which sessions have a paused queue card. A string, so a row subscribed to it
 *  re-renders only when that set changes (a prompt moving to another runtime), not on every session-state publish. */
export const $stuckPromptInputsKey = computed([$sessionStates, $pausedQueueSessionIds], (states, paused) =>
  [
    Object.entries(states)
      .filter(([, s]) => s?.needsInput)
      .map(([runtimeId, s]) => `${runtimeId}=${s?.storedSessionId ?? ''}`)
      .join(','),
    paused.join(',')
  ].join('|')
)

/** Where a red edge goes besides desktop.log: the session's own gateway (`POST /api/sessions/<id>/stripe-report`),
 *  which keeps it for `hermes sessions outcome <id>`. Set by the sidebar; a no-op until then (tests, early boot). */
export type StripeReasonReporter = (
  row: StripeTraceRow,
  stripe: null | SessionStatusStripe,
  previous: null | SessionStatusStripe,
  reasons: readonly string[]
) => Promise<unknown> | void

let stripeReasonReporter: null | StripeReasonReporter = null

export const setStripeReasonReporter = (reporter: null | StripeReasonReporter): void => {
  stripeReasonReporter = reporter
}

/** [daveey] The reason string a non-red stripe reports while the row has an open human ask and is not ⌘B-parked (the
 *  backend logs `stripe-invariant-violation <sid>` for it). `stripe` is already the result of `sessionStatusStripe`,
 *  so this fires only when some caller bypassed the invariant. The invariant governs live rows only: an archived or
 *  offline-cached row has no stripe at all (null), which is not a violation. */
function stripeInvariantViolation(
  row: StripeTraceRow,
  stripe: null | SessionStatusStripe,
  nowSeconds: number,
  live: boolean
): null | string {
  if (!live || row.archived || row.offline_cached || stripe === 'stuck' || stripe === 'blocked') {
    return null
  }

  const asks = kanbanStripeCounts(row, nowSeconds).blocked + (row.review_wait === 'stuck' ? 1 : 0)

  return asks > 0 ? `needs_you:${asks}` : null
}

function reportStripeReason(
  row: StripeTraceRow,
  stripe: null | SessionStatusStripe,
  previous: null | SessionStatusStripe,
  reasons: readonly string[]
): void {
  if (!stripeReasonReporter || row.offline_cached) {
    return
  }

  try {
    void Promise.resolve(stripeReasonReporter(row, stripe, previous, reasons)).catch(() => undefined)
  } catch {
    // Diagnostics only.
  }
}

/** Build the desktop.log line for one transition. Exported for tests. */
export function formatStripeTransition(
  row: StripeTraceRow,
  from: null | SessionStatusStripe,
  to: null | SessionStatusStripe,
  dotState: SessionDotState,
  suppressed = 0,
  reasons: readonly string[] = []
): string {
  const fields: [string, unknown][] = [
    ['conn', row.connection_id ?? 'primary'],
    ['profile', row.profile],
    ['state', dotState],
    ['kanban_active', row.kanban_active],
    ['kanban_parked', row.kanban_parked],
    ['kanban_held', row.kanban_held],
    ['kanban_stalled', row.kanban_stalled],
    ['kanban_blocked', row.kanban_blocked],
    ['kanban_waiting_on', row.kanban_waiting_on],
    ['kanban_observed_at', row.kanban_observed_at],
    ['goal_active', row.goal_active],
    ['cron_active', row.cron_active],
    ['background_active', row.background_active],
    ['delegating', $delegatingSessionIds.get().includes(row.id)],
    ['bg_running', $backgroundRunningSessionIds.get().includes(row.id)],
    ['source', rowListSource(row)],
    ['outcome', row.outcome?.kind],
    ['muted', row.muted]
  ]

  if (suppressed > 0) {
    fields.push(['suppressed', suppressed])
  }

  if (reasons.length > 0) {
    fields.push(['stuck', `[${reasons.map(r => r.replace(/\s+/g, '_')).join(';')}]`])
  }

  return `stripe-transition ${row.id} ${from ?? 'none'}->${to ?? 'none'} ${fields
    .map(([key, value]) => `${key}=${traceValue(value)}`)
    .join(' ')}`
}

/** Remember `stripe` for the row and, when it differs from the last one seen, write a desktop.log line. The first
 *  sighting only records (a cold sidebar would otherwise log every row). At most
 *  `STRIPE_TRANSITION_LOG_CAP_PER_MIN` lines per session per rolling minute; the next line after a capped burst
 *  reports how many were skipped. Never throws. */
export function recordStripeTransition(
  row: StripeTraceRow,
  stripe: null | SessionStatusStripe,
  dotState: SessionDotState,
  now: number = Date.now(),
  live = true
): void {
  try {
    const key = `${row.connection_id ?? ''}\u0000${row.profile ?? ''}\u0000${row.id}`
    const entry = stripeTrace.get(key)
    const reasons = stripe === 'stuck' ? stuckReasons(row, dotState, now / 1000) : []
    const reasonsKey = reasons.join('|')
    const violation = stripeInvariantViolation(row, stripe, now / 1000, live)

    if (!entry) {
      stripeTrace.set(key, { emitted: [], reasonsKey, stripe, suppressed: 0 })

      // [daveey] A row already red when first seen (after a restart, a reconnect) is exactly the red nobody can
      // explain later: report it, though the log still skips first sightings.
      if (stripe === 'stuck') {
        reportStripeReason(row, stripe, null, reasons)
      } else if (violation) {
        reportStripeReason(row, stripe, null, [violation])
      }

      return
    }

    // A red row whose cause changed (the prompt answered but a card blocked) is a new red: log and report it.
    if (entry.stripe === stripe && (stripe !== 'stuck' || entry.reasonsKey === reasonsKey)) {
      return
    }

    const from = entry.stripe
    entry.stripe = stripe
    entry.reasonsKey = reasonsKey
    entry.emitted = entry.emitted.filter(at => now - at < STRIPE_TRANSITION_WINDOW_MS)

    if (violation) {
      reportStripeReason(row, stripe, from, [violation])
    }

    if (entry.emitted.length >= STRIPE_TRANSITION_LOG_CAP_PER_MIN) {
      entry.suppressed += 1

      return
    }

    entry.emitted.push(now)
    const line = formatStripeTransition(row, from, stripe, dotState, entry.suppressed, reasons)
    entry.suppressed = 0

    if (stripe === 'stuck' || from === 'stuck') {
      reportStripeReason(row, stripe, from, reasons)
    }

    if (typeof window !== 'undefined') {
      const win = new URLSearchParams(window.location.search).get('win') || 'main'
      window.hermesDesktop?.logLine?.(`[sidebar win=${win}] ${line}`)
    }
  } catch {
    // Diagnostics only: a trace failure must never break a row render.
  }
}
