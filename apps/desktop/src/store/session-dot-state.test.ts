import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionInfo } from '@/types/hermes'

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
  hasLiveTurn,
  sessionStatusStripe,
  showsRunningArc,
  unreadSessionCount,
  withoutBackgroundProcess,
  withRowActivity
} from './session-dot-state'
import { clearAllSessionStates, publishSessionState } from './session-states'
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

  it('leaves a session that is not running unmarked', () => {
    expect(showsRunningArc('background')).toBe(false)
    expect(showsRunningArc('idle')).toBe(false)
    expect(showsRunningArc('unread')).toBe(false)
  })
})

describe('sessionStatusStripe', () => {
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
    const unseenRow = { kanban_unseen: 1 }
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

  it('excludes work that outlived the turn', () => {
    expect(hasLiveTurn('background')).toBe(false)
    expect(hasLiveTurn('unread')).toBe(false)
  })
})

describe('$delegatingSessionIds', () => {
  const subagent = (status: SubagentProgress['status']): SubagentProgress => ({
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
    $subagentsBySession.set({ 'runtime-fresh': [subagent('queued')] })

    expect($delegatingSessionIds.get()).toContain('runtime-fresh')
  })
})

const storedRow = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

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
    const row = { goal_active: false, kanban_active: 0, kanban_blocked: 1, kanban_unseen: 1 }

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

    expect(withRowActivity('idle', { goal_active: false, kanban_active: 0 })).toBe('idle')
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
    expect(withRowActivity('idle', { kanban_active: 0, kanban_unseen: 1 })).toBe('unread')
    expect(withRowActivity('idle', { kanban_active: 1, kanban_unseen: 1 })).toBe('background')
    expect(withRowActivity('idle', { kanban_active: 0 })).toBe('idle')
  })
})
