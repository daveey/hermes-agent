import { cleanStores } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { projectExecutionObservation } from '@/lib/execution-observations'
import type { SessionInfo } from '@/types/hermes'

import fixture from '../../../../schemas/fixtures/execution-observations-v1.json'

import { executionHoldsRow, rowExecution } from './execution-observation-row'
import { $kanbanObservationTick } from './kanban-observation'
import { $messagingSessions, $sessions, setMessagingSessions, setSessions } from './session'
import {
  $executionRowKeys,
  $kanbanBusySessionIds,
  $sessionDotStateById,
  kanbanStripeCounts,
  sessionStatusStripe,
  withRowActivity
} from './session-dot-state'
import { sessionHasStatusStripe } from './sidebar-visible-order'

// Activity verdicts come ONLY from the shared fixture's `expected.state`; status paint stays unknown without a published protocol.
// Nothing here re-calls the projector to decide an expectation.
type Patch = { set?: Record<string, unknown>; delete?: string[] }

interface Case extends Patch {
  id: string
  now?: unknown
  source_class?: string
  expected: Record<string, unknown>
  expected_identity?: unknown
}

const clone = <T>(v: T): T => structuredClone(v)

function apply(value: Record<string, unknown>, patch: Patch) {
  for (const [path, replacement] of Object.entries(patch.set ?? {})) {
    const keys = path.split('.')
    let parent = value

    for (const key of keys.slice(0, -1)) {
      parent = parent[key] as Record<string, unknown>
    }

    parent[keys.at(-1)!] = clone(replacement)
  }

  for (const path of patch.delete ?? []) {
    const keys = path.split('.')
    let parent = value

    for (const key of keys.slice(0, -1)) {
      parent = parent[key] as Record<string, unknown>
    }

    delete parent[keys.at(-1)!]
  }

  return value
}

const obs = (patch: Patch = {}) => apply(clone(fixture.base) as Record<string, unknown>, patch)
const base = fixture.base as Record<string, unknown>

const row = (extra: Partial<SessionInfo>, at: number): SessionInfo =>
  ({
    id: 's1',
    kanban_active: 0,
    kanban_observed_at: at,
    message_count: 2,
    source: 'cli',
    started_at: 0,
    title: 's1',
    ...extra
  }) as SessionInfo

const classify = (r: SessionInfo) => {
  const counts = kanbanStripeCounts(r)
  const dot = withRowActivity('idle', r)

  return { dot, stripe: sessionStatusStripe(dot, true, false, counts.blocked, false, null, 0, counts.waiting, counts.stalled) }
}

const AT = 1001

afterEach(() => {
  vi.useRealTimers()
  cleanStores($kanbanBusySessionIds, $sessionDotStateById, $executionRowKeys)
  setSessions([])
  setMessagingSessions([])
})

describe('sidebar classification of the shared execution fixture', () => {
  const cases = (fixture.cases as unknown as Case[]).filter(
    c =>
      typeof c.expected.state === 'string' &&
      (c.source_class ?? 'sidebar') === 'sidebar' &&
      c.expected_identity === undefined &&
      (c.now === undefined || (typeof c.now === 'number' && c.now >= 0))
  )

  it('covers a broad slice of the fixture, including every sidebar-visible state', () => {
    expect(cases.length).toBeGreaterThan(150)
    expect(new Set(cases.map(c => c.expected.state))).toEqual(
      new Set(['running', 'queued', 'waiting', 'needs_user', 'done', 'failed', 'cancelled', 'unknown'])
    )
  })

  for (const test of cases) {
    it(test.id, () => {
      const now = (test.now as number | undefined) ?? AT

      vi.useFakeTimers()
      vi.setSystemTime(now * 1000)
      const r = row({ execution_observations: [obs(test)] }, now)
      const { dot, stripe } = classify(r)
      expect(stripe).toBe('attention') // No published status: local evidence cannot become status authority.
      const state = test.expected.state as string

      if (state === 'running' || state === 'queued') {
        // A structured ask the fixture says survives (even beside running work) is outstanding: red, never erased.
        expect(dot).toBe('background')
        expect(rowExecution(r)).toMatchObject({ live: 1, unknown: 0 })
      } else if (state === 'waiting') {
        expect(dot).toBe('background')
      } else if (state === 'needs_user') {
        expect(dot).toBe('background')
      } else if (state === 'done' || state === 'failed' || state === 'cancelled') {
        // Terminal evidence never holds a row; the legacy stamps (none here) decide what it paints.
        expect(dot).toBe('idle')
        expect(executionHoldsRow(rowExecution(r))).toBe(false)

      } else {
        // Unknown is held, visibly, and is never zero work or done.
        expect(rowExecution(r)?.unknown).toBe(1)
        expect(dot).toBe('background')
        expect(stripe).not.toBe('done')
      }
    })
  }
})

describe('sidebar execution wiring', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(AT * 1000)
  })

  it('treats absent or empty observations as no activity evidence; missing published status stays unknown', () => {
    for (const execution_observations of [undefined, []]) {
      const r = row(execution_observations ? { execution_observations } : {}, AT)

      expect(rowExecution(r)).toBeNull()
      expect(classify(r)).toEqual({ dot: 'idle', stripe: 'attention' })
    }

    // Legacy staleness still holds the row with no observations at all.
    expect(withRowActivity('idle', row({}, AT - 181))).toBe('background')
  })

  it('makes legacy live card counts from an older producer (no observations array) explicit unknown', () => {
    const legacy = row({ kanban_active: 2, kanban_blocked: 1, kanban_parked: 1 }, AT)

    expect(rowExecution(legacy)).toMatchObject({ live: 0, unknown: 1 })
    expect(withRowActivity('idle', legacy)).toBe('background')
    expect(rowExecution(row({ kanban_active: 1, kanban_parked: 1 }, AT))).toBeNull()  // legacy wait stays a wait
    // A current producer sends the array; its own records decide, the legacy count adds no unknown.
    expect(rowExecution(row({ execution_observations: [], kanban_active: 1 }, AT))).toBeNull()
  })

  it('keeps the existing 180 s kanban boundary independent of observations', () => {
    const fresh = [
      obs({
        set: {
          delivery_receipt: { disposition: 'delivered', evidence_ref: 'completed:42', identity: base.identity, verified: true },
          state: 'done'
        }
      })
    ]

    expect(withRowActivity('idle', row({ execution_observations: fresh }, AT - 179))).toBe('idle')
    expect(withRowActivity('idle', row({ execution_observations: fresh }, AT - 181))).toBe('background')
  })

  it('never lets observations erase legacy work or a legacy ask', () => {
    const done = [obs({ set: { state: 'done' } })]

    expect(withRowActivity('idle', row({ execution_observations: done, kanban_active: 1 }, AT))).toBe('background')
    expect(kanbanStripeCounts(row({ execution_observations: done, kanban_blocked: 1 }, AT)).blocked).toBe(1)
  })

  it.each(['kanban.dispatch', 'kanban.capacity'])('does not count %s capacity-only diagnostics as work', source => {
    const diagnostic = obs({ set: { source, diagnostics: [{ code: 'capacity.profile_limit' }] } })
    const r = row({ execution_observations: [diagnostic] }, AT)

    expect(rowExecution(r)).toBeNull()
    expect(classify(r)).toEqual({ dot: 'idle', stripe: 'attention' })
    expect(diagnostic.diagnostics).toEqual([{ code: 'capacity.profile_limit' }])
  })

  it('keeps mixed capacity diagnostics separate from fresh work and arbitrary unknown work', () => {
    const diagnostics = { source: 'kanban.dispatch', state: 'unknown', diagnostics: [{ code: 'capacity.global_limit' }] }
    const r = row({ execution_observations: [diagnostics, obs(), { source: 'other.unknown' }] }, AT)
    const before = clone(r)

    expect(rowExecution(r)).toMatchObject({ live: 1, unknown: 1 })
    expect(r).toEqual(before)
    expect(classify(r).dot).toBe('background')
  })

  it('shows known fresh work and a partial unknown together', () => {
    const r = row(
      { execution_observations: [obs(), obs({ set: { 'identity.work_id': 'task-b', state: 'unknown', unknown_reason: 'source_offline' } })] },
      AT
    )

    expect(rowExecution(r)).toMatchObject({ live: 1, unknown: 1 })
    expect(classify(r).dot).toBe('background')
  })

  it('retains the historical receipt while expired current completion stays visibly unknown', () => {
    const receipt = { disposition: 'delivered', evidence_ref: 'completed:42', identity: base.identity, verified: true }
    const completed = obs({ set: { delivery_receipt: receipt, state: 'done' } })
    const r = row({ execution_observations: [completed] }, 1181)

    vi.setSystemTime(1181 * 1000)
    expect(projectExecutionObservation(completed, 1181, 'sidebar')).toMatchObject({
      delivery_receipt: receipt, last_known_state: 'done', state: 'unknown'
    })
    expect(rowExecution(r)).toMatchObject({ unknown: 1 })
    expect(executionHoldsRow(rowExecution(r))).toBe(true)
    expect(classify(r)).toEqual({ dot: 'background', stripe: 'attention' })
    expect(completed.delivery_receipt).toEqual(receipt)
  })

  it('does not reclassify missing published status when local completion evidence expires', () => {
    const receipt = { disposition: 'delivered', evidence_ref: 'completed:42', identity: base.identity, verified: true }
    setSessions([row({ execution_observations: [obs({ set: { delivery_receipt: receipt, state: 'done', valid_until: 1010 } })] }, AT)])
    expect(sessionHasStatusStripe('s1')).toBe(true)
    vi.setSystemTime(1011_000)
    $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    expect(sessionHasStatusStripe('s1')).toBe(true)
  })

  it('expires by time alone: running becomes unknown with no new row, without moving the row to done', () => {
    const r = row({ execution_observations: [obs()] }, AT)

    expect(rowExecution(r, AT)).toMatchObject({ live: 1, unknown: 0 })
    expect(rowExecution(r, 1180)).toMatchObject({ live: 0, unknown: 1 })
    expect(executionHoldsRow(rowExecution(r, 1180))).toBe(true)
  })

  it('republishes per-row evidence only when a verdict changes, driven by the existing tick', () => {
    setSessions([row({ execution_observations: [obs()] }, AT)])
    const seen: Record<string, string>[] = []
    const stop = $executionRowKeys.listen(v => seen.push(v))

    vi.setSystemTime(1100 * 1000)
    $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    expect(seen).toHaveLength(0)

    vi.setSystemTime(1181 * 1000)
    $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    expect(seen).toHaveLength(1)
    expect(seen[0].s1).toBe('0/0/0/1/0')
    stop()
  })

  it('does not erase an outstanding request when the asking entry expires', () => {
    const asking = obs({
      set: {
        state: 'needs_user',
        human_request: { action: 'Pick', evidence: 'request:1', interaction: 'reply', scope: 'task', why_human: 'Preference' },
        wait: {
          kind: 'human',
          owner: { gateway: 'edge', profile: 'work' },
          predicate: 'reply',
          since: 1000,
          target: { board: 'tasks', gateway: 'edge', kind: 'card', profile: 'work', work_id: 'task-a' }
        }
      }
    })

    const live = row({ execution_observations: [asking] }, AT)

    expect(classify(live)).toEqual({ dot: 'background', stripe: 'attention' })

    vi.setSystemTime(1500 * 1000)
    const expired = row({ execution_observations: [asking] }, 1500)

    expect(rowExecution(expired)).toMatchObject({ needsUser: 0, outstanding: 1, unknown: 1 })
    expect(classify(expired)).toEqual({ dot: 'background', stripe: 'attention' })
  })

  it('keeps legacy turn_live working and adds no certainty when evidence is unusable', () => {
    const r = row({ execution_observations: [{ junk: true }], turn_live: true }, AT)

    expect(withRowActivity('idle', r)).toBe('working')
    expect(rowExecution(r)?.unknown).toBe(1)
  })

  it('holds a listed row off done through the real busy store, for every list', () => {
    setSessions([row({ execution_observations: [obs()] }, AT)])
    setMessagingSessions([row({ execution_observations: [obs({ set: { state: 'unknown' } })], id: 'm1' }, AT)])

    expect([...$kanbanBusySessionIds.get()].sort()).toEqual(['m1', 's1'])
    expect($sessions.get()).toHaveLength(1)
    expect($messagingSessions.get()).toHaveLength(1)
  })

  it('holds a row whose evidence lacks a valid execution identity (unknown, not zero)', () => {
    const r = row({ execution_observations: [obs({ set: { 'identity.run_id': null } })] }, AT)

    expect(rowExecution(r)?.unknown).toBe(1)
  })
})
