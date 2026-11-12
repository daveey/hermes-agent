import { afterEach, describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { kanbanStripeCounts, sessionStatusStripe, withRowActivity } from './session-dot-state'
import { $unavailableWorkSources, reportWorkSourceRead, workSourceUnavailable } from './session-work-source'

const done = { kind: 'done', source: 'derived', at: 1000 } as const

const row = { id: 'chat', connection_id: 'gateway', profile: 'default', message_count: 2,
  kanban_observed_at: Date.now() / 1000, outcome: done } as SessionInfo

function painted(input: SessionInfo, unavailable = false) {
  const state = withRowActivity('idle', input)
  const counts = kanbanStripeCounts(input)

  return sessionStatusStripe(state, true, false, counts.blocked, false, null, 0,
    counts.waiting, counts.stalled, input.outcome, unavailable)
}

afterEach(() => $unavailableWorkSources.set({}))

describe('idle completion fails closed at the composed stripe boundary', () => {
  it.each(['tasks', 'process', 'subagent', 'lineage'] as const)('retains %s failure across turn end, then recovers on the same source', source => {
    const report = { connectionId: 'gateway', profile: 'default', storedSessionId: 'chat', source }
    reportWorkSourceRead({ ...report, available: false })
    expect(painted(row, workSourceUnavailable('gateway', 'default', ['chat']))).toBe('waiting')
    expect(row.outcome).toEqual(done)
    expect(workSourceUnavailable('other', 'default', ['chat'])).toBe(false)
    expect(workSourceUnavailable('gateway', 'other', ['chat'])).toBe(false)
    expect(workSourceUnavailable('gateway', 'default', ['other'])).toBe(false)
    reportWorkSourceRead({ ...report, available: true })
    expect(painted(row, workSourceUnavailable('gateway', 'default', ['chat']))).toBe('done')
  })

  it('does not let another successful source clear the failed one', () => {
    reportWorkSourceRead({ connectionId: 'gateway', profile: 'default', storedSessionId: 'chat', source: 'tasks', available: false })
    reportWorkSourceRead({ connectionId: 'gateway', profile: 'default', storedSessionId: 'chat', source: 'process', available: true })
    expect(painted(row, workSourceUnavailable('gateway', 'default', ['chat']))).toBe('waiting')
  })

  it.each([
    { kanban_unknown: true },
    { kanban_observed_at: undefined },
    { kanban_remote_stale: 1 },
    { kanban_active: 1, kanban_parked: 1 },
    { kanban_active: 1, kanban_parked: 0 },
    { background_active: 1 }
  ])('never paints stored done over unavailable or unfinished row evidence %j', evidence => {
    expect(painted({ ...row, ...evidence })).not.toBe('done')
  })

  it('preserves known completion and stronger user/live-turn states', () => {
    expect(painted(row)).toBe('done')
    expect(sessionStatusStripe('working', true, false, 0, false, null, 0, 0, 0, done, true)).toBeNull()
    expect(sessionStatusStripe('idle', true, true, 0, false, null, 0, 0, 0, done, true)).toBe('blocked')
    expect(sessionStatusStripe('needs-input', true, false, 0, false, null, 0, 0, 0, done, true)).toBe('stuck')
    expect(sessionStatusStripe('idle', true, false, 0, false, null, 0, 0, 0,
      { kind: 'decision', source: 'derived', at: 1000 }, true)).toBe('stuck')
  })
})
