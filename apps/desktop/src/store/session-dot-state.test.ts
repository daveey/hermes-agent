import { cleanStores } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionInfo } from '@/types/hermes'

import fixture from '../../../../schemas/fixtures/execution-observations-v1.json'

import { $queuedPromptsBySession } from './composer-queue'
import { $kanbanObservationTick } from './kanban-observation'
import {
  $cronSessions,
  $messagingSessions,
  $sessions,
  $unreadFinishedSessionIds,
  markAllSessionsRead,
  setCronSessions,
  setMessagingSessions,
  setSessions
} from './session'
import {
  $backgroundActiveSessionIds,
  $backgroundProcessesBySessionId,
  $delegatingSessionIds,
  $goalActiveSessionIds,
  $kanbanBusySessionIds,
  $kanbanUnseenSessionIds,
  $sessionDotStateById,
  $unreadSessionCount,
  backgroundDotLabel,
  hasLiveTurn,
  kanbanStripeCounts,
  markSessionListRefreshReason,
  noteSessionListSource,
  recordStripeTransition,
  resetStripeTransitionLog,
  sessionStatusStripe,
  setStripeReasonReporter,
  showsRunningArc,
  STRIPE_TRANSITION_LOG_CAP_PER_MIN,
  stuckReasons,
  takeSessionListRefreshReason,
  unreadSessionCount,
  withoutBackgroundProcess,
  withRowActivity
} from './session-dot-state'
import { $sessionStates, clearAllSessionStates, publishSessionState } from './session-states'
import { $unreadWriteGuard } from './session-unread-remote'
import { $subagentsBySession, type SubagentProgress } from './subagents'

describe('showsRunningArc', () => {
  it('keeps the arc when an authoritative turn goes quiet', () => {
    expect(showsRunningArc('working')).toBe(true)
    expect(showsRunningArc('stalled')).toBe(true)
  })

  it('yields to the needs-input treatment rather than running both', () => {
    expect(showsRunningArc('needs-input')).toBe(false)
  })

  it('keeps the arc while subagents or background processes outlive the turn', () => {
    expect(showsRunningArc('background')).toBe(true)
  })

  it('leaves a session that is not running unmarked', () => {
    expect(showsRunningArc('idle')).toBe(false)
    expect(showsRunningArc('unread')).toBe(false)
  })
})

describe('sessionStatusStripe', () => {
  it('keeps stored outcomes authoritative during background work, but not a live own turn', () => {
    const decision = { kind: 'decision', summary: 'Pick A or B' } as const

    expect(hasLiveTurn('background')).toBe(true)
    expect(showsRunningArc('background')).toBe(true)
    expect(sessionStatusStripe('background', true, false, 0, false, null, 0, 0, 0, decision as never)).toBe('stuck')
    expect(sessionStatusStripe('working', true, false, 0, false, null, 0, 0, 0, decision as never)).toBeNull()
  })

  // [daveey] A chat that handed its work to kanban cards waits on them: grey
  // `waiting`, never green and never blank, until they finish.
  it('is waiting (grey) while the chat has open kanban cards', () => {
    for (const state of ['background', 'idle', 'unread', 'draft'] as const) {
      expect(sessionStatusStripe(state, true, false, 0, false, null, 0, 1)).toBe('waiting')
    }

    expect(sessionStatusStripe('working', true, false, 0, false, null, 0, 1)).toBeNull()
    expect(sessionStatusStripe('needs-input', true, false, 0, false, null, 0, 1)).toBe('stuck')
    // A card blocked on the user → red with it.
    expect(sessionStatusStripe('background', true, false, 1, false, null, 0, 1)).toBe('stuck')
    expect(sessionStatusStripe('idle', true, true, 0, false, null, 0, 1)).toBe('blocked')
    expect(sessionStatusStripe('idle', true, false, 0, true, null, 0, 1)).toBe('periodic')
  })

  it('all cards done with results unseen → green (unread); none open, all done → green', () => {
    // Completion is green only with a fresh authoritative observation.
    expect(withRowActivity('idle', { kanban_unseen: 1 })).toBe('background')
    const unseenRow = { kanban_observed_at: Date.now() / 1000, kanban_unseen: 1 }
    const state = withRowActivity('idle', unseenRow)
    expect(state).toBe('unread')
    expect(sessionStatusStripe(state, true, false, 0, false, null, 0, 0)).toBe('done')
    expect(sessionStatusStripe('idle', true, false, 0, false, null, 0, 0)).toBe('done')
  })

  it('paints done (green) when a finished turn waits on the user, read or not', () => {
    expect(sessionStatusStripe('unread', true)).toBe('done')
    expect(sessionStatusStripe('idle', true)).toBe('done')
  })

  it('paints blocked (grey) for a user-parked session, over any live state', () => {
    for (const state of ['idle', 'unread', 'needs-input', 'working', 'draft'] as const) {
      expect(sessionStatusStripe(state, true, true)).toBe('blocked')
    }
  })

  // [daveey] kanban_attention turns a would-be green into amber, and only that.
  it('paints attention (amber) instead of done when a finished card dropped work', () => {
    expect(sessionStatusStripe('idle', true, false, 0, false, null, 1)).toBe('attention')
    expect(sessionStatusStripe('unread', true, false, 0, false, null, 2)).toBe('attention')
    expect(sessionStatusStripe('idle', true, false, 0, false, null, 0)).toBe('done')
    // Louder or quieter states keep their own stripe.
    expect(sessionStatusStripe('needs-input', true, false, 0, false, null, 1)).toBe('stuck')
    expect(sessionStatusStripe('idle', true, false, 1, false, null, 1)).toBe('stuck')
    expect(sessionStatusStripe('idle', true, true, 0, false, null, 1)).toBe('blocked')
    expect(sessionStatusStripe('working', true, false, 0, false, null, 1)).toBeNull()
  })

  it('paints stuck (red) when an unfinished turn needs help', () => {
    expect(sessionStatusStripe('needs-input', true)).toBe('stuck')
    expect(sessionStatusStripe('stalled', true)).toBe('stuck')
  })

  it('paints nothing while working, or before any turn', () => {
    for (const state of ['working', 'background', 'draft'] as const) {
      expect(sessionStatusStripe(state, true)).toBeNull()
    }

    expect(sessionStatusStripe('idle', false)).toBeNull()
  })

  it('paints stuck (red) when kanban cards the session created are blocked, unless a turn is working', () => {
    for (const state of ['idle', 'unread', 'background', 'draft'] as const) {
      expect(sessionStatusStripe(state, true, false, 2)).toBe('stuck')
    }

    expect(sessionStatusStripe('working', true, false, 1)).toBeNull()
    expect(sessionStatusStripe('idle', true, true, 1)).toBe('blocked')
    expect(sessionStatusStripe('idle', true, false, 0)).toBe('done')
  })

  it('paints periodic (grey loop) for a session owning a live cron job, unless a prompt waits', () => {
    for (const state of ['idle', 'unread', 'working', 'background', 'stalled'] as const) {
      expect(sessionStatusStripe(state, true, false, 0, true)).toBe('periodic')
    }

    expect(sessionStatusStripe('idle', true, false, 3, true)).toBe('periodic')
    expect(sessionStatusStripe('needs-input', true, false, 0, true)).toBe('stuck')
    expect(sessionStatusStripe('idle', true, true, 0, true)).toBe('blocked')
  })

  it('[daveey] a PR waiting on my review is red; on others for <24h is grey; working wins', () => {
    for (const state of ['idle', 'unread', 'background'] as const) {
      expect(sessionStatusStripe(state, true, false, 0, false, 'stuck')).toBe('stuck')
      expect(sessionStatusStripe(state, true, false, 0, false, 'parked')).toBe('blocked')
    }

    expect(sessionStatusStripe('working', true, false, 0, false, 'stuck')).toBeNull()
    expect(sessionStatusStripe('working', true, false, 0, false, 'parked')).toBeNull()
    expect(sessionStatusStripe('needs-input', true, false, 0, false, 'parked')).toBe('stuck')
    expect(sessionStatusStripe('idle', true, true, 0, false, 'stuck')).toBe('blocked')
    expect(sessionStatusStripe('idle', true, false, 0, false, null)).toBe('done')
    expect(sessionStatusStripe('idle', true, false, 0, false, undefined)).toBe('done')
  })
})

describe('hasLiveTurn', () => {
  it('counts a turn waiting on an answer as still live', () => {
    expect(hasLiveTurn('needs-input')).toBe(true)
  })

  it('covers everything the arc covers', () => {
    for (const state of ['background', 'idle', 'needs-input', 'stalled', 'unread', 'working'] as const) {
      expect(hasLiveTurn(state) || !showsRunningArc(state)).toBe(true)
    }
  })

  it('excludes a finished session', () => {
    expect(hasLiveTurn('unread')).toBe(false)
  })
})

describe('$delegatingSessionIds', () => {
  const subagent = (status: SubagentProgress['status']): SubagentProgress => ({
    executionObservations: status === 'running' ? [{
      version: 1, identity: { gateway: 'edge', profile: 'default', session: 'owner', kind: 'subagent', work_id: 'sub-1', generation: 'session=sub-1' },
      owner: { gateway: 'edge', profile: 'default', session: 'owner' }, state: 'running', source: 'subagent.activity',
      source_revision: 'sub-1@1000', observed_at: Date.now() / 1000, valid_until: Date.now() / 1000 + 180, evidence_refs: ['sub-1']
    }] : [],
    id: 'sub-1',
    parentId: null,
    goal: 'do a thing',
    status,
    taskCount: 1,
    taskIndex: 0,
    startedAt: 0,
    updatedAt: 0,
    filesRead: [],
    filesWritten: [],
    stream: []
  })

  afterEach(() => {
    clearAllSessionStates()
    $subagentsBySession.set({})
    $sessions.set([])
  })

  it('claims the stored id while a subagent is running after the parent turn ended', () => {
    publishSessionState('runtime-1', { ...createClientSessionState('stored-1'), busy: false })
    $subagentsBySession.set({ 'runtime-1': [subagent('running')] })

    expect($delegatingSessionIds.get()).toContain('stored-1')
  })

  it('drops the session once every subagent reaches a terminal status', () => {
    publishSessionState('runtime-1', { ...createClientSessionState('stored-1'), busy: false })
    $subagentsBySession.set({ 'runtime-1': [subagent('running')] })
    $subagentsBySession.set({ 'runtime-1': [subagent('completed')] })

    expect($delegatingSessionIds.get()).not.toContain('stored-1')
  })

  it('falls back to the runtime id for a not-yet-persisted conversation', () => {
    $subagentsBySession.set({ 'runtime-fresh': [subagent('running')] })

    expect($delegatingSessionIds.get()).toContain('runtime-fresh')
  })
})

const storedRow = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, kanban_observed_at: Date.now() / 1000, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

describe('persisted unread (backend watermark)', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  it('claims unread for a session whose row carries unread: true', () => {
    setSessions([storedRow('s1', { unread: true })])

    expect($sessionDotStateById.get()['s1']).toBe('unread')
  })

  it('keeps draft weaker than persisted unread', () => {
    // A blank tile (no busy, no messages, message_count 0) is a draft; the
    // persisted unread claim must speak over it.
    setSessions([storedRow('s1', { message_count: 0, unread: true })])
    publishSessionState('rt1', createClientSessionState('s1'))

    expect($sessionDotStateById.get()['s1']).toBe('unread')
  })

  // [daveey] A settled session nothing references releases its transcript (messages: []). When its row is not in
  // `$sessions` (a project-tree row past the recents page) that cold state must not read as "nothing sent yet".
  it('a released transcript is not a draft, even with no listed row', () => {
    // No busy→idle transition here: that would claim `unread` and mask the draft question.
    const loaded = { ...createClientSessionState('s1'), messages: [{ id: 'm1', parts: [], role: 'user' }] as never }
    publishSessionState('rt1', loaded)
    expect($sessionDotStateById.get()['s1']).not.toBe('draft')
    publishSessionState('rt1', { ...loaded })
    expect($sessionStates.get().rt1?.messages).toEqual([])
    expect($sessionDotStateById.get()['s1']).not.toBe('draft')

    // Control: an open tab nothing was ever sent to, with no listed row, is still a draft.
    publishSessionState('rt2', createClientSessionState('s2'))
    expect($sessionDotStateById.get()['s2']).toBe('draft')
  })

  it('lets working outrank persisted unread', () => {
    setSessions([storedRow('s1', { unread: true })])
    publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

    expect($sessionDotStateById.get()['s1']).toBe('working')
  })

  it('fences a stale page with the write guard', () => {
    const guard = new Map<string, { at: number; value: boolean }>()
    guard.set('s1', { at: Date.now(), value: true })
    $unreadWriteGuard.set(guard)

    // A page issued before our PATCH still says read — keep OUR value.
    setSessions([storedRow('s1', { unread: false })])
    expect($sessionDotStateById.get()['s1']).toBe('unread')

    // The guard expires: the page wins and the dot drops.
    const expired = new Map<string, { at: number; value: boolean }>()
    expired.set('s1', { at: Date.now() - 60_000, value: true })
    $unreadWriteGuard.set(expired)
    expect($sessionDotStateById.get()['s1']).not.toBe('unread')
  })

  it('leaves a row alone when the backend omits the flag (older runtime)', () => {
    setSessions([storedRow('s1')])

    expect($sessionDotStateById.get()['s1'] ?? 'idle').not.toBe('unread')
  })
})

describe('unreadSessionCount', () => {
  it('counts listed unread rows and skips archived', () => {
    expect(
      unreadSessionCount({ a: 'unread', b: 'working', c: 'unread' }, [
        { id: 'a' },
        { id: 'b' },
        { archived: true, id: 'c' },
        { id: 'missing' }
      ])
    ).toBe(1)
  })

  it('does not count alias keys that are not listed rows', () => {
    expect(unreadSessionCount({ tip: 'unread', root: 'unread' }, [{ id: 'tip' }])).toBe(1)
  })
})

describe('$unreadSessionCount (titlebar badge)', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $cronSessions.set([])
    $messagingSessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $cronSessions.set([])
    $messagingSessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  it('does not count an unread cron session — cron runs finish unwatched by design (#93552)', () => {
    setCronSessions([storedRow('cron-1', { source: 'cron' })])
    $unreadFinishedSessionIds.set(['cron-1'])

    // The cron row itself still paints unread in the sidebar cron section...
    expect($sessionDotStateById.get()['cron-1']).toBe('unread')
    // ...but the titlebar badge stays quiet.
    expect($unreadSessionCount.get()).toBe(0)
  })

  it('counts an unread regular session', () => {
    setSessions([storedRow('reg-1', { unread: true })])

    expect($unreadSessionCount.get()).toBe(1)
  })

  it('counts regular + messaging but never cron', () => {
    setSessions([storedRow('reg-1', { unread: true })])
    setMessagingSessions([storedRow('msg-1')])
    setCronSessions([storedRow('cron-1', { source: 'cron' })])
    $unreadFinishedSessionIds.set(['msg-1', 'cron-1'])

    expect($unreadSessionCount.get()).toBe(2)
  })

  it('mark-all-read clears regular and cron unread alike', () => {
    setSessions([storedRow('reg-1')])
    setCronSessions([storedRow('cron-1', { source: 'cron' })])
    $unreadFinishedSessionIds.set(['reg-1', 'cron-1'])

    expect($unreadSessionCount.get()).toBe(1)
    expect($sessionDotStateById.get()['cron-1']).toBe('unread')

    markAllSessionsRead()

    expect($unreadSessionCount.get()).toBe(0)
    expect($sessionDotStateById.get()['cron-1']).not.toBe('unread')
  })
})

describe('kanban work in flight ($kanbanBusySessionIds)', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
  })

  it('reads a finished turn with running kanban tasks as background, not done', () => {
    setSessions([storedRow('s1', { kanban_active: 2, unread: true }), storedRow('s2')])

    expect($kanbanBusySessionIds.get()).toEqual(['s1'])
    expect($sessionDotStateById.get()['s1']).toBe('background')
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()
    // Control: a plain finished session still gets its green stripe.
    expect(sessionStatusStripe($sessionDotStateById.get()['s2'] ?? 'idle', true)).toBe('done')
  })

  it.each([undefined, 1, Infinity, Date.now() / 1000 + 60])('keeps absent or stale observations off done: %s', observed => {
    setSessions([storedRow('s1', { kanban_observed_at: observed })])

    expect($kanbanBusySessionIds.get()).toContain('s1')
    expect(withRowActivity('idle', { kanban_observed_at: observed } as never)).toBe('background')
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()
  })

  it('expires a zero-work observation even when no list refresh arrives', () => {
    // Earlier get() calls mount real timers until nanostores' delayed unmount.
    // Remount under the fake clock; isolated execution already passes.
    cleanStores($kanbanBusySessionIds, $kanbanObservationTick)
    vi.useFakeTimers()
    const stop = $kanbanBusySessionIds.subscribe(() => {})

    try {
      setSessions([storedRow('s1')])
      expect($kanbanBusySessionIds.get()).toEqual([])
      vi.advanceTimersByTime(200_000)
      expect($kanbanBusySessionIds.get()).toContain('s1')
    } finally {
      stop()
      vi.advanceTimersByTime(2_000)
      vi.useRealTimers()
    }
  })

  it('does not become done when the goal and running cards finish but a blocked card remains', () => {
    setSessions([storedRow('s1', { goal_active: true, kanban_active: 1, kanban_blocked: 1, unread: true })])
    expect($sessionDotStateById.get().s1).toBe('background')

    setSessions([storedRow('s1', { goal_active: false, kanban_active: 0, kanban_blocked: 1, unread: true })])
    expect($kanbanBusySessionIds.get()).toEqual(['s1'])
    expect($sessionDotStateById.get().s1).toBe('background')
    expect(sessionStatusStripe($sessionDotStateById.get().s1, true)).not.toBe('done')

    setSessions([storedRow('s1', { goal_active: false, kanban_active: 0, kanban_blocked: 0, unread: true })])
    expect($kanbanBusySessionIds.get()).toEqual([])
    expect(sessionStatusStripe($sessionDotStateById.get().s1, true)).toBe('done')
  })

  it('folds blocked-only project rows over finished/unseen results after goal completion', () => {
    const row = { goal_active: false, kanban_active: 0, kanban_blocked: 1, kanban_observed_at: Date.now() / 1000, kanban_unseen: 1 }

    for (const state of ['idle', 'unread', 'draft'] as const) {
      expect(withRowActivity(state, row)).toBe('background')
      expect(sessionStatusStripe(withRowActivity(state, row), true)).not.toBe('done')
    }

    for (const state of ['working', 'stalled', 'needs-input'] as const) {
      expect(withRowActivity(state, row)).toBe(state)
    }
  })

  it('goes back to done once the tasks finish', () => {
    setSessions([storedRow('s1', { kanban_active: 1 })])
    setSessions([storedRow('s1', { kanban_active: 0 })])

    expect($kanbanBusySessionIds.get()).toEqual([])
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBe('done')
  })

  it('keeps a row whose kanban read failed (kanban_unknown) off done, and a stale remote count busy', () => {
    setSessions([
      storedRow('s1', { kanban_unknown: true, unread: true }),
      storedRow('s2', { kanban_active: 13, kanban_remote_stale: 13 }),
      storedRow('s3')
    ])

    expect($kanbanBusySessionIds.get()).toEqual(['s1', 's2'])
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()
    expect(sessionStatusStripe($sessionDotStateById.get()['s2'] ?? 'idle', true)).toBeNull()
    expect(withRowActivity('idle', { kanban_unknown: true })).toBe('background')
    // Control: a session with no kanban stamps still finishes green.
    expect(sessionStatusStripe($sessionDotStateById.get()['s3'] ?? 'idle', true)).toBe('done')
  })

  it('yields to a live turn', () => {
    setSessions([storedRow('s1', { kanban_active: 1 })])
    publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

    expect($sessionDotStateById.get()['s1']).toBe('working')
  })
})

describe('backend-registered background process ($backgroundActiveSessionIds)', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
  })

  it('reads a finished turn with a live background process as background, not done', () => {
    setSessions([storedRow('s1', { background_active: 1, unread: true }), storedRow('s2')])

    expect($backgroundActiveSessionIds.get()).toEqual(['s1'])
    expect($sessionDotStateById.get()['s1']).toBe('background')
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()
    // Control: a plain finished session still gets its green stripe.
    expect(sessionStatusStripe($sessionDotStateById.get()['s2'] ?? 'idle', true)).toBe('done')
  })

  it('goes back to done once the process exits', () => {
    setSessions([storedRow('s1', { background_active: 1 })])
    setSessions([storedRow('s1')])

    expect($backgroundActiveSessionIds.get()).toEqual([])
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBe('done')
  })

  it('yields to a live turn', () => {
    setSessions([storedRow('s1', { background_active: 2 })])
    publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

    expect($sessionDotStateById.get()['s1']).toBe('working')
  })
})

// [daveey] The row's process badge: which live processes hold it off green.
describe('background processes a row lists ($backgroundProcessesBySessionId)', () => {
  const sleep = { command: 'sleep 600', id: 'proc_a', started_at: 1_700_000_000 }

  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
  })

  it('lists the process of a row held only by it, and drops it when the process exits', () => {
    setSessions([storedRow('s1', { background_active: 1, background_processes: [sleep] }), storedRow('s2')])

    expect($backgroundProcessesBySessionId.get()).toEqual({ s1: [sleep] })
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()

    // The next list poll no longer stamps it: badge gone, row green.
    setSessions([storedRow('s1'), storedRow('s2')])

    expect($backgroundProcessesBySessionId.get()).toEqual({})
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBe('done')
  })

  it('a confirmed Stop drops the process from the row at once', () => {
    const other = { command: 'gh run watch 1', id: 'proc_b' }
    const row = storedRow('s1', { background_active: 2, background_processes: [sleep, other] })

    const one = withoutBackgroundProcess(row, 'proc_a')
    expect(one.background_processes).toEqual([other])
    expect(one.background_active).toBe(1)

    const none = withoutBackgroundProcess(one, 'proc_b')
    setSessions([none])

    expect(none.background_active).toBeUndefined()
    expect($backgroundProcessesBySessionId.get()).toEqual({})
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBe('done')
    // Control: an id the row doesn't list leaves it untouched.
    expect(withoutBackgroundProcess(row, 'proc_zzz')).toBe(row)
  })

  it('falls back to count-only placeholders against an older backend', () => {
    setSessions([storedRow('s1', { background_active: 2 })])

    expect($backgroundProcessesBySessionId.get()['s1']?.map(p => p.id)).toEqual(['#0', '#1'])
  })
})

describe('standing goal in flight ($goalActiveSessionIds)', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
  })

  it('reads a finished turn with an active goal as background, not done', () => {
    setSessions([storedRow('s1', { goal_active: true, unread: true }), storedRow('s2')])

    expect($goalActiveSessionIds.get()).toEqual(['s1'])
    expect($sessionDotStateById.get()['s1']).toBe('background')
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()
    // Control: a plain finished session still gets its green stripe.
    expect(sessionStatusStripe($sessionDotStateById.get()['s2'] ?? 'idle', true)).toBe('done')
  })

  it('goes back to done once the goal ends', () => {
    setSessions([storedRow('s1', { goal_active: true })])
    setSessions([storedRow('s1')])

    expect($goalActiveSessionIds.get()).toEqual([])
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBe('done')
  })

  it('yields to a live turn', () => {
    setSessions([storedRow('s1', { goal_active: true })])
    publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

    expect($sessionDotStateById.get()['s1']).toBe('working')
  })
})

describe('withRowActivity', () => {
  it('claims background over idle/unread/draft when the row carries an activity stamp', () => {
    for (const state of ['idle', 'unread', 'draft'] as const) {
      expect(withRowActivity(state, { goal_active: true })).toBe('background')
      expect(withRowActivity(state, { kanban_active: 1 })).toBe('background')
      expect(withRowActivity(state, { background_active: 2 })).toBe('background')
    }
  })

  it('leaves louder states and stamp-less rows alone', () => {
    for (const state of ['working', 'stalled', 'needs-input'] as const) {
      expect(withRowActivity(state, { goal_active: true })).toBe(state)
    }

    expect(withRowActivity('idle', { goal_active: false, kanban_active: 0, kanban_observed_at: Date.now() / 1000 })).toBe('idle')
  })

  // [daveey] A backend-woken Kanban close-out turn in a chat this window never opened (or one on a gateway other
  // than the active one) reaches the renderer only as the row's `turn_live` stamp.
  it('a running turn the backend reports (turn_live) reads as working over every non-live state', () => {
    for (const state of ['idle', 'unread', 'draft', 'background'] as const) {
      expect(withRowActivity(state, { turn_live: true })).toBe('working')
      expect(hasLiveTurn(withRowActivity(state, { turn_live: true, kanban_active: 2 }))).toBe(true)
    }

    // A live turn the stream already knows keeps its own, more specific state.
    for (const state of ['working', 'stalled', 'needs-input'] as const) {
      expect(withRowActivity(state, { turn_live: true })).toBe(state)
    }

    // An offline cached copy proves nothing about now.
    expect(withRowActivity('idle', { offline_cached: true, turn_live: true })).toBe('idle')
  })

  // [daveey] The row is authoritative about its own history: a project-tree row older than the recents page is not in
  // `$sessions`, so a cold (transcript-released) client state for it read as a draft and the row lost its green.
  it('a draft claim yields to a row that has messages', () => {
    const fresh = { kanban_observed_at: Date.now() / 1000 }

    expect(withRowActivity('draft', { ...fresh, message_count: 207 })).toBe('idle')
    expect(sessionStatusStripe(withRowActivity('draft', { ...fresh, message_count: 207 }), true)).toBe('done')
    // Still background when the row carries work.
    expect(withRowActivity('draft', { ...fresh, goal_active: true, message_count: 3 })).toBe('background')

    // Controls: a genuinely unsent row (or one whose count is unknown) stays a draft.
    expect(withRowActivity('draft', { ...fresh, message_count: 0 })).toBe('draft')
    expect(withRowActivity('draft', fresh)).toBe('draft')
  })

  it('a decision outcome yields to a running close-out turn and paints red again once it ends', () => {
    const decision = { kind: 'decision', summary: 'Pick A or B' } as const
    const row = { kanban_active: 0, kanban_observed_at: Date.now() / 1000 }

    const during = withRowActivity('idle', { ...row, turn_live: true })
    expect(during).toBe('working')
    expect(sessionStatusStripe(during, true, false, 0, false, null, 0, 0, 0, decision as never)).toBeNull()

    // Control: no live turn, so the stored decision still paints the red stripe.
    const after = withRowActivity('idle', row)
    expect(after).toBe('idle')
    expect(sessionStatusStripe(after, true, false, 0, false, null, 0, 0, 0, decision as never)).toBe('stuck')
  })
})

// [daveey] The background dot keeps its claim on unknown work, but its label must not assert "running".
describe('backgroundDotLabel', () => {
  const fresh = () => ({ kanban_observed_at: Date.now() / 1000 })
  const unknownObs = { source: 'unavailable.work' }

  const live = () => {
    const now = Date.now() / 1000

    return { ...fixture.base, observed_at: now, valid_until: now + 120 }
  }

  // [daveey] A fresh dependency wait (the fixture's `dependency` case) under its own card identity.
  const waitingObs = () => ({
    ...live(),
    ...fixture.cases.find(c => c.id === 'dependency')!.set,
    identity: { ...fixture.base.identity, work_id: 'task-waiting' }
  })

  it('is unknown when the claim rests only on unknown or unfresh evidence', () => {
    const rows = [
      { ...fresh(), kanban_unknown: true },
      { ...fresh(), execution_observations: [], kanban_active: 2, kanban_parked: 2, kanban_remote_stale: 2 },
      { kanban_observed_at: undefined },
      { ...fresh(), execution_observations: [unknownObs], kanban_active: 1, kanban_parked: 1 },
      // An older backend's unparked cards are discovered as unknown, never as certain running.
      { ...fresh(), kanban_active: 1 },
      // A known wait beside unknown work does not vouch for the rest.
      { ...fresh(), execution_observations: [waitingObs(), unknownObs], kanban_active: 2, kanban_parked: 2 }
    ]

    for (const row of rows) {
      expect(withRowActivity('idle', row as never)).toBe('background')
      expect(backgroundDotLabel(row as never)).toBe('unknown')
    }
  })

  it('says running when a process, subagent, goal or running card exists', () => {
    expect(backgroundDotLabel({ kanban_unknown: true, background_active: 1 })).toBe('running')
    expect(backgroundDotLabel({ kanban_unknown: true, goal_active: true })).toBe('running')
    expect(backgroundDotLabel({ kanban_unknown: true }, true)).toBe('running')
    expect(
      backgroundDotLabel({ ...fresh(), execution_observations: [live(), unknownObs], kanban_active: 2 } as never)
    ).toBe('running')
    expect(
      backgroundDotLabel({ ...fresh(), execution_observations: [live(), waitingObs()], kanban_active: 2, kanban_parked: 1 } as never)
    ).toBe('running')
  })

  it('says waiting when the only open work is known waiting (fresh, all parked)', () => {
    const rows = [
      { ...fresh(), execution_observations: [waitingObs()], kanban_active: 1, kanban_parked: 1 },
      { ...fresh(), execution_observations: [], kanban_active: 2, kanban_parked: 2 }
    ]

    for (const row of rows) {
      expect(backgroundDotLabel(row as never)).toBe('waiting')
    }
  })

  it('keeps the running label for blocked or unparked work (not a known wait)', () => {
    expect(backgroundDotLabel({ ...fresh(), kanban_active: 0, kanban_blocked: 1 })).toBe('running')
    expect(
      backgroundDotLabel({ ...fresh(), execution_observations: [], kanban_active: 2, kanban_blocked: 1, kanban_parked: 2 } as never)
    ).toBe('running')
    expect(
      backgroundDotLabel({ ...fresh(), execution_observations: [waitingObs()], kanban_active: 2, kanban_parked: 1 } as never)
    ).toBe('running')
  })

  // [daveey] PR #2234 review: a held card (todo behind a human ask) is red in `kanbanStripeCounts`, so the label
  // must not call the row "waiting" while the stripe says blocked.
  it('keeps the running label while a held card paints the stripe red', () => {
    const rows = [
      { ...fresh(), execution_observations: [], kanban_active: 2, kanban_held: 1, kanban_parked: 1 },
      { ...fresh(), execution_observations: [], kanban_active: 1, kanban_held: 1, kanban_parked: 0 },
      { ...fresh(), execution_observations: [waitingObs()], kanban_active: 2, kanban_held: 1, kanban_parked: 1 }
    ]

    for (const row of rows) {
      expect(kanbanStripeCounts(row as never).blocked).toBeGreaterThan(0)
      expect(backgroundDotLabel(row as never)).toBe('running')
    }
  })
})

// [daveey] A chat that was not loaded anywhere when its cards finished.
describe('kanban results waiting for an unloaded session ($kanbanUnseenSessionIds)', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
    $unreadWriteGuard.set(new Map())
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $unreadFinishedSessionIds.set([])
  })

  it('all cards done plus an unseen result paints green (unread -> done stripe)', () => {
    setSessions([storedRow('s1', { kanban_active: 0, kanban_unseen: 2 }), storedRow('s2')])

    expect($kanbanUnseenSessionIds.get()).toEqual(['s1'])
    expect($sessionDotStateById.get()['s1']).toBe('unread')
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBe('done')
  })

  it('cards still running keep the row in background (no stripe), even with a result waiting', () => {
    setSessions([storedRow('s1', { kanban_active: 1, kanban_unseen: 1 })])

    expect($sessionDotStateById.get()['s1']).toBe('background')
    expect(sessionStatusStripe($sessionDotStateById.get()['s1'] ?? 'idle', true)).toBeNull()
  })

  it('an orphan-reaped idle session with no cards is green, never working or stalled', () => {
    setSessions([storedRow('s1', { ended_at: 1, end_reason: 'startup_orphan_reap' } as never)])

    const state = $sessionDotStateById.get()['s1'] ?? 'idle'

    expect(state === 'working' || state === 'stalled').toBe(false)
    expect(sessionStatusStripe(state, true)).toBe('done')
  })

  it('withRowActivity turns an idle project-tree row with a waiting result green', () => {
    expect(withRowActivity('idle', { kanban_active: 0, kanban_unseen: 1, kanban_observed_at: Date.now() / 1000 })).toBe('unread')
    expect(withRowActivity('idle', { kanban_active: 1, kanban_unseen: 1 })).toBe('background')
    expect(withRowActivity('idle', { kanban_active: 0, kanban_observed_at: Date.now() / 1000 })).toBe('idle')
  })
})

// [daveey] Parked vs live kanban work: a chat whose open cards are only parked (scheduled on a timed/event wake,
// blocked on a non-human kind, queued behind an unfinished parent) waits (grey); one whose queue waits on a human
// ask upstream is stuck (red); only live cards (running/ready/review/promotable) read as working (no stripe).
describe('kanbanStripeCounts', () => {
  it('live cards are working: no waiting count, background dot, no stripe', () => {
    const row = { kanban_observed_at: Date.now() / 1000, kanban_active: 3, kanban_parked: 1 }
    expect(kanbanStripeCounts(row)).toEqual({ blocked: 0, external: 0, externalDown: 0, externalUnknown: 0, stalled: 0, waiting: 0 })
    const state = withRowActivity('idle', row)
    expect(state).toBe('background')
    expect(sessionStatusStripe(state, true, false, 0, false, null, 0, kanbanStripeCounts(row).waiting)).toBeNull()
  })

  it('parked-only work waits (grey) and does not paint the dot as running', () => {
    const row = { kanban_observed_at: Date.now() / 1000, kanban_active: 2, kanban_parked: 2 }
    const counts = kanbanStripeCounts(row)
    expect(counts).toEqual({ blocked: 0, external: 0, externalDown: 0, externalUnknown: 0, stalled: 0, waiting: 2 })
    const state = withRowActivity('idle', row)
    expect(state).toBe('idle')
    expect(sessionStatusStripe(state, true, false, counts.blocked, false, null, 0, counts.waiting)).toBe('waiting')
  })

  it('a queue held behind a human ask is stuck (red)', () => {
    const row = { kanban_observed_at: Date.now() / 1000, kanban_active: 1, kanban_parked: 0, kanban_held: 1 }
    const counts = kanbanStripeCounts(row)
    expect(counts).toEqual({ blocked: 1, external: 0, externalDown: 0, externalUnknown: 0, stalled: 0, waiting: 0 })
    expect(sessionStatusStripe(withRowActivity('idle', row), true, false, counts.blocked, false, null, 0, counts.waiting)).toBe('stuck')
  })

  it('held plus live: a live turn still is not red, but a background row is', () => {
    const row = { kanban_observed_at: Date.now() / 1000, kanban_active: 2, kanban_parked: 0, kanban_held: 1 }
    const counts = kanbanStripeCounts(row)
    expect(sessionStatusStripe('working', true, false, counts.blocked, false, null, 0, counts.waiting)).toBeNull()
    expect(sessionStatusStripe(withRowActivity('idle', row), true, false, counts.blocked, false, null, 0, counts.waiting)).toBe('stuck')
  })

  it('a backend without kanban_parked keeps the old behaviour (all open cards wait)', () => {
    const row = { kanban_observed_at: Date.now() / 1000, kanban_active: 2, kanban_blocked: 1 }
    expect(kanbanStripeCounts(row)).toEqual({ blocked: 1, external: 0, externalDown: 0, externalUnknown: 0, stalled: 0, waiting: 2 })
    expect(withRowActivity('idle', row)).toBe('background')
  })

  it('no kanban work: zero counts', () => {
    expect(kanbanStripeCounts({})).toEqual({ blocked: 0, external: 0, externalDown: 0, externalUnknown: 0, stalled: 0, waiting: 0 })
  })

  // [daveey] Agent-owned parks that nothing has produced for over 2 h: amber `stalled`, distinct from grey waiting
  // and red needs-you; a human ask still wins, and live work still reads as working.
  it('parked work with a stalled card paints amber stalled, not grey waiting', () => {
    const row = { kanban_observed_at: Date.now() / 1000, kanban_active: 3, kanban_parked: 3, kanban_stalled: 1 }
    const counts = kanbanStripeCounts(row)
    expect(counts).toEqual({ blocked: 0, external: 0, externalDown: 0, externalUnknown: 0, stalled: 1, waiting: 3 })
    const state = withRowActivity('idle', row)
    expect(sessionStatusStripe(state, true, false, counts.blocked, false, null, 0, counts.waiting, counts.stalled)).toBe(
      'stalled'
    )
  })

  it('a human ask (red) beats stalled, and live work hides it', () => {
    const held = { kanban_observed_at: Date.now() / 1000, kanban_active: 2, kanban_parked: 1, kanban_held: 1, kanban_stalled: 1 }
    const h = kanbanStripeCounts(held)
    expect(sessionStatusStripe(withRowActivity('idle', held), true, false, h.blocked, false, null, 0, h.waiting, h.stalled)).toBe(
      'stuck'
    )
    const live = { kanban_observed_at: Date.now() / 1000, kanban_active: 2, kanban_parked: 1, kanban_stalled: 1 }
    expect(kanbanStripeCounts(live)).toEqual({ blocked: 0, external: 0, externalDown: 0, externalUnknown: 0, stalled: 0, waiting: 0 })
  })

  it('reads kanban_external / kanban_external_down stamps when cards are absent', () => {
    const row = {
      kanban_active: 1,
      kanban_external: 1,
      kanban_external_down: 0,
      kanban_observed_at: Date.now() / 1000,
      kanban_parked: 1
    }

    expect(kanbanStripeCounts(row)).toEqual({
      blocked: 0,
      external: 1,
      externalDown: 0,
      externalUnknown: 0,
      stalled: 0,
      waiting: 1
    })
  })
})

// [daveey] ⌘B > live turn > stuck/red (incl external_down) > external > stalled > waiting > done
describe('sessionStatusStripe external-run precedence', () => {
  const idle = 'idle' as const

  it('⌘B (userBlocked) beats every kanban colour including external_down', () => {
    expect(sessionStatusStripe(idle, true, true, 0, false, null, 0, 1, 1, null, false, 1, 1)).toBe('blocked')
  })

  it('a live working turn has no stripe even with external_down', () => {
    expect(sessionStatusStripe('working', true, false, 0, false, null, 0, 1, 1, null, false, 1, 1)).toBeNull()
  })

  it('stuck/red (human kanban block) beats external and external_down', () => {
    expect(sessionStatusStripe(idle, true, false, 1, false, null, 0, 1, 1, null, false, 1, 1)).toBe('stuck')
  })

  it('external_down is red at the stuck tier and beats light-blue external, stalled, waiting', () => {
    expect(sessionStatusStripe(idle, true, false, 0, false, null, 0, 1, 1, null, false, 1, 1)).toBe('external_down')
  })

  it('external (sky) beats stalled and waiting', () => {
    expect(sessionStatusStripe(idle, true, false, 0, false, null, 0, 1, 1, null, false, 1, 0)).toBe('external')
  })

  it('stalled still beats waiting when there is no external work', () => {
    expect(sessionStatusStripe(idle, true, false, 0, false, null, 0, 1, 1, null, 0, 0)).toBe('stalled')
  })

  it('waiting when parked with no external/stalled/red', () => {
    expect(sessionStatusStripe(idle, true, false, 0, false, null, 0, 1, 0, null, 0, 0)).toBe('waiting')
  })

  it('done when the chat has turns and nothing is waiting', () => {
    expect(sessionStatusStripe(idle, true, false, 0, false, null, 0, 0, 0, null, 0, 0)).toBe('done')
  })

  it('absent external fields keep today: parked-only is waiting, not external', () => {
    expect(sessionStatusStripe(idle, true, false, 0, false, null, 0, 2, 0)).toBe('waiting')
  })

  it('backgroundDotLabel is external when kanban_external is stamped', () => {
    const row = {
      kanban_active: 1,
      kanban_external: 1,
      kanban_observed_at: Date.now() / 1000,
      kanban_parked: 1
    }

    expect(backgroundDotLabel(row)).toBe('external')
  })

  it('backgroundDotLabel is external_down for down-only rows', () => {
    const row = {
      kanban_active: 1,
      kanban_external_down: 1,
      kanban_observed_at: Date.now() / 1000,
      kanban_parked: 1
    }

    expect(backgroundDotLabel(row)).toBe('external_down')
  })

  it('cards without count stamps still count as background work', () => {
    const now = Date.now() / 1000

    const row = {
      kanban_external_cards: [
        {
          external: {
            adapter: 'github_run',
            health: 'ok',
            job: 'running',
            next_due_at: now + 300,
            observed_at: now,
            reason: "job 'tests'",
            service: 'GitHub Actions'
          },
          task_id: 't_a'
        }
      ],
      kanban_observed_at: now,
      message_count: 4
    }

    expect(withRowActivity('idle', row)).toBe('background')
  })
})

describe('recordStripeTransition', () => {
  const logLine = vi.fn()
  let previousBridge: unknown

  const row = {
    connection_id: 'hub-ec2',
    goal_active: false,
    id: '20261027_085427_ac4078',
    kanban_active: 1,
    kanban_observed_at: 1793140000,
    kanban_parked: 1,
    kanban_waiting_on: ['t_01a17e8a62a6ef7b'],
    profile: 'default'
  }

  beforeEach(() => {
    resetStripeTransitionLog()
    logLine.mockReset()
    previousBridge = (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = { logLine }
  })

  afterEach(() => {
    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = previousBridge
    setSessions([])
    resetStripeTransitionLog()
  })

  it('logs exactly one line per transition of a done -> waiting -> done flip, with the row inputs', () => {
    setSessions([{ ...row, message_count: 3 } as unknown as SessionInfo])
    markSessionListRefreshReason('sessions.changed')
    noteSessionListSource(takeSessionListRefreshReason())

    recordStripeTransition(row, 'done', 'idle', 1_000)
    expect(logLine).not.toHaveBeenCalled()

    recordStripeTransition(row, 'waiting', 'background', 2_000)
    recordStripeTransition(row, 'waiting', 'background', 2_500)
    expect(logLine).toHaveBeenCalledTimes(1)

    recordStripeTransition(row, 'done', 'idle', 3_000)
    expect(logLine).toHaveBeenCalledTimes(2)

    const [first, second] = logLine.mock.calls.map(call => String(call[0]))

    expect(first).toContain(`stripe-transition ${row.id} done->waiting`)
    expect(second).toContain(`stripe-transition ${row.id} waiting->done`)

    for (const field of [
      'conn=hub-ec2',
      'kanban_active=1',
      'kanban_parked=1',
      'kanban_waiting_on=[t_01a17e8a62a6ef7b]',
      'goal_active=false',
      'kanban_observed_at=1793140000',
      'delegating=false',
      'bg_running=false',
      'source=sessions.changed'
    ]) {
      expect(second).toContain(field)
    }

    expect(second).toContain('state=idle')
    expect(first).toContain('state=background')
  })

  it('emits nothing while the stripe is unchanged (control)', () => {
    for (let i = 0; i < 20; i += 1) {
      recordStripeTransition(row, 'waiting', 'background', 1_000 + i)
    }

    expect(logLine).not.toHaveBeenCalled()
  })

  it('tags rows outside the sidebar page by origin: project tree, offline cache, plain refetch', () => {
    recordStripeTransition(row, 'waiting', 'background', 1_000)
    recordStripeTransition(row, 'done', 'idle', 2_000)
    expect(String(logLine.mock.calls[0][0])).toContain('source=project-tree')

    const cached = { ...row, id: 'cached', offline_cached: true }
    recordStripeTransition(cached, 'done', 'idle', 1_000)
    recordStripeTransition(cached, null, 'idle', 2_000)
    expect(String(logLine.mock.calls[1][0])).toContain('cached done->none')
    expect(String(logLine.mock.calls[1][0])).toContain('source=cache')

    // [daveey] A replica/boot fallback row is named as such: it carries no activity stamps (cron_active, kanban_*).
    const fallback = { ...row, id: 'fallback', offline_synced_at: 9_000 }
    recordStripeTransition(fallback, 'periodic', 'background', 1_000)
    recordStripeTransition(fallback, null, 'background', 2_000)
    expect(String(logLine.mock.calls[2][0])).toContain('fallback periodic->none')
    expect(String(logLine.mock.calls[2][0])).toContain('source=fallback')
    expect(String(logLine.mock.calls[2][0])).toContain('cron_active=-')

    expect(takeSessionListRefreshReason()).toBe('list-refetch')
  })

  it('caps a flapping session per minute and reports what it skipped', () => {
    const stripes = ['done', 'waiting'] as const
    recordStripeTransition(row, 'done', 'idle', 0)

    for (let i = 1; i <= STRIPE_TRANSITION_LOG_CAP_PER_MIN + 4; i += 1) {
      recordStripeTransition(row, stripes[i % 2], 'idle', i * 1_000)
    }

    expect(logLine).toHaveBeenCalledTimes(STRIPE_TRANSITION_LOG_CAP_PER_MIN)

    recordStripeTransition(row, 'stuck', 'stalled', 70_000)
    expect(logLine).toHaveBeenCalledTimes(STRIPE_TRANSITION_LOG_CAP_PER_MIN + 1)
    expect(String(logLine.mock.lastCall?.[0])).toContain('suppressed=4')
  })
})

// [daveey] selfheal desktop-54ae373894 (t_01a1d3142a47913f): a chat sat red with no blocked card and no visible
// question, and its agent, asked "why are you red", guessed wrong. Every red verdict now carries its inputs.
describe('stuckReasons and the red report', () => {
  const report = vi.fn()
  const logLine = vi.fn()
  let previousBridge: unknown
  const row = { connection_id: 'hub-ec2', id: 'red-chat', profile: 'default' }

  beforeEach(() => {
    resetStripeTransitionLog()
    report.mockReset()
    logLine.mockReset()
    setStripeReasonReporter(report)
    previousBridge = (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = { logLine }
  })

  afterEach(() => {
    setStripeReasonReporter(null)
    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = previousBridge
    clearAllSessionStates()
    $queuedPromptsBySession.set({})
    setSessions([])
    resetStripeTransitionLog()
  })

  it('names the runtime holding the prompt, a paused queue, or says unknown', () => {
    publishSessionState('rt-dead', { ...createClientSessionState('red-chat'), needsInput: true })
    publishSessionState('rt-other', { ...createClientSessionState('other-chat'), needsInput: true })
    expect(stuckReasons(row, 'needs-input')).toEqual(['needs-input:prompt runtime=rt-dead'])

    $queuedPromptsBySession.set({ 'red-chat': [{ attachments: [], id: 'q1', serverStatus: 'unknown', text: 'hi' }] } as never)
    expect(stuckReasons(row, 'needs-input')).toEqual(['needs-input:prompt runtime=rt-dead', 'needs-input:paused-queue'])

    clearAllSessionStates()
    $queuedPromptsBySession.set({})
    expect(stuckReasons(row, 'needs-input')).toEqual(['needs-input:unknown'])
  })

  it('names a quiet turn, blocked cards with their ids, a review wait and a decision outcome', () => {
    expect(stuckReasons(row, 'stalled')).toEqual(['stalled'])
    expect(
      stuckReasons({ ...row, kanban_blocked: 1, kanban_blocked_on: ['default/t_a'], kanban_held: 2 }, 'background')
    ).toEqual(['kanban-blocked:3 on=default/t_a held=2'])
    expect(stuckReasons({ ...row, review_wait: 'stuck', review_wait_pr: 'o/r#9' }, 'idle')).toEqual(['review:o/r#9'])
    expect(stuckReasons({ ...row, review_wait: 'stuck' }, 'idle')).toEqual(['review:?'])
    expect(stuckReasons({ ...row, outcome: { kind: 'decision' } } as never, 'idle')).toEqual(['outcome:decision'])
    // A working turn beats cards, reviews and the stored outcome, exactly as sessionStatusStripe does.
    expect(
      stuckReasons({ ...row, kanban_blocked: 1, outcome: { kind: 'decision' }, review_wait: 'stuck' } as never, 'working')
    ).toEqual([])
    expect(stuckReasons({ ...row, review_wait: 'parked', outcome: { kind: 'done' } } as never, 'idle')).toEqual([])
  })

  it('reports a red first sighting, each new red cause, and the way out; logs the reasons', () => {
    publishSessionState('rt-dead', { ...createClientSessionState('red-chat'), needsInput: true })

    recordStripeTransition(row, 'stuck', 'needs-input', 1_000)
    expect(report).toHaveBeenCalledTimes(1)
    expect(report.mock.calls[0].slice(1)).toEqual(['stuck', null, ['needs-input:prompt runtime=rt-dead']])
    expect(logLine).not.toHaveBeenCalled() // first sightings stay out of the log, as before

    recordStripeTransition(row, 'stuck', 'needs-input', 1_500) // same cause: nothing
    expect(report).toHaveBeenCalledTimes(1)

    recordStripeTransition({ ...row, kanban_blocked: 1, kanban_blocked_on: ['t_b'] }, 'stuck', 'background', 2_000)
    expect(report).toHaveBeenCalledTimes(2)
    expect(report.mock.calls[1].slice(1)).toEqual(['stuck', 'stuck', ['kanban-blocked:1 on=t_b']])
    expect(String(logLine.mock.lastCall?.[0])).toContain('stuck->stuck')
    expect(String(logLine.mock.lastCall?.[0])).toContain('stuck=[kanban-blocked:1_on=t_b]')

    recordStripeTransition(row, 'done', 'idle', 3_000)
    expect(report.mock.calls[2].slice(1)).toEqual(['done', 'stuck', []])

    recordStripeTransition(row, 'waiting', 'background', 4_000) // not red on either side: log only
    expect(report).toHaveBeenCalledTimes(3)
    expect(logLine).toHaveBeenCalledTimes(3)
  })

  it('never reports an offline-cached row and survives a throwing reporter', () => {
    recordStripeTransition({ ...row, offline_cached: true }, 'stuck', 'stalled', 1_000)
    expect(report).not.toHaveBeenCalled()

    setStripeReasonReporter(() => {
      throw new Error('boom')
    })
    expect(() => recordStripeTransition({ ...row, id: 'x' }, 'stuck', 'stalled', 1_000)).not.toThrow()
  })
})
