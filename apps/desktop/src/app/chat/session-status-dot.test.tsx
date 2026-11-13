import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { rowExecution } from '@/store/execution-observation-row'
import { activeGatewayProfileKey, gatewayRouteState, isActivePrimary } from '@/store/gateway'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $liveWorkRoster, setLiveWorkRosterSource } from '@/store/live-work-roster'
import { $sessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import fixture from '../../../../../schemas/fixtures/execution-observations-v1.json'

import { SessionStatusDot } from './session-status-dot'

vi.mock('@/i18n', async () => {
  const { enSessionRowStatus } = await import('@/i18n/en_session_row_status')

  return {
    useI18n: () => ({
      t: {
        sidebar: {
          row: {
            ...enSessionRowStatus,
            backgroundRunning: 'Running in background',
            draftSession: 'Draft',
            finishedUnread: 'Finished',
            needsInput: 'Needs input',
            sessionRunning: 'Running',
            waitingForAnswer: 'Waiting for answer'
          }
        }
      }
    })
  }
})

const RUNNING = '[role="status"][aria-label="Running in background"]'
const UNKNOWN = '[role="status"][aria-label="Open work, current status unknown"]'
const WAITING = '[role="status"][aria-label="Open work, waiting"]'

// The render clock sits inside the fixture's validity window (observed 1000, valid until 1600).
const NOW = 1001
const unknownObs = { source: 'unavailable.work' }

const waitingObs = {
  ...fixture.base,
  ...fixture.cases.find(c => c.id === 'dependency')!.set,
  identity: { ...fixture.base.identity, work_id: 'task-waiting' }
}

/** The roster source `rowExecution` reads for a row with no known owner. */
const ownSource = () => (isActivePrimary() ? gatewayRouteState(null, activeGatewayProfileKey()).key : 'ambient')

/** A remote roster read reporting a running card (no process/subagent count, so no other store claims the row). */
const rosterRunningCard = (sessionId: string) =>
  setLiveWorkRosterSource(ownSource(), [{ background: 0, execution_observations: [fixture.base], session_id: sessionId, subagents: 0 }])

const dot = (session: null | Partial<SessionInfo>, id = session?.id ?? null) =>
  render(<SessionStatusDot session={session as null | SessionInfo} storedSessionId={id} />).container

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW * 1000)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  $liveWorkRoster.set({})
  $sessions.set([])
  $sessionTiles.set([])
})

// [daveey] PR #2154 review follow-ups: the dot evaluates every store its row verdict reads, falls back to the
// listed row, and names known waiting work.
describe('SessionStatusDot live inputs (no tick lag)', () => {
  it('relabels unknown -> running as soon as the roster reports a running card, without the 15 s tick', () => {
    const tick = $kanbanObservationTick.get()

    const c = dot({
      execution_observations: [unknownObs],
      id: 'lag-label',
      kanban_active: 1,
      kanban_observed_at: NOW,
      kanban_parked: 1,
      message_count: 4
    })

    expect(c.querySelector(UNKNOWN)).not.toBeNull()

    act(() => rosterRunningCard('lag-label'))

    expect(rowExecution({ id: 'lag-label' } as SessionInfo)?.live).toBe(1)
    expect($kanbanObservationTick.get()).toBe(tick)
    expect(c.querySelector(RUNNING)).not.toBeNull()
    expect(c.querySelector(UNKNOWN)).toBeNull()
  })

  // [daveey] PR #2234 review: the owner route picks the roster source. A tile that names the owner moves the row to
  // that source's evidence at once, not on the next 15 s tick.
  it('relabels as soon as a tile names the owner whose roster reports a running card', () => {
    const tick = $kanbanObservationTick.get()
    const otherSource = gatewayRouteState(null, 'owner-elsewhere').key

    expect(otherSource).not.toBe(ownSource())
    setLiveWorkRosterSource(otherSource, [
      { background: 0, execution_observations: [fixture.base], session_id: 'owned', subagents: 0 }
    ])

    const c = dot({
      execution_observations: [unknownObs],
      id: 'owned',
      kanban_active: 1,
      kanban_observed_at: NOW,
      kanban_parked: 1,
      message_count: 4
    })

    expect(c.querySelector(UNKNOWN)).not.toBeNull()

    act(() => $sessionTiles.set([{ ownerProfile: 'owner-elsewhere', storedSessionId: 'owned' }]))

    expect(rowExecution({ id: 'owned' } as SessionInfo)?.live).toBe(1)
    expect($kanbanObservationTick.get()).toBe(tick)
    expect(c.querySelector(RUNNING)).not.toBeNull()
    expect(c.querySelector(UNKNOWN)).toBeNull()
  })

  it('repaints an idle tree row as background as soon as the roster reports its running card', () => {
    const c = dot({ id: 'lag-state', kanban_observed_at: NOW, message_count: 4 })

    expect(c.querySelector('[role="status"]')).toBeNull()

    act(() => rosterRunningCard('lag-state'))

    expect(c.querySelector(RUNNING)).not.toBeNull()
  })
})

describe('SessionStatusDot with no session row', () => {
  for (const stamp of [{ background_active: 1 }, { goal_active: true }]) {
    it(`reads the listed row for a store-claimed ${Object.keys(stamp)[0]} instead of saying unknown`, () => {
      $sessions.set([{ id: 'listed', kanban_observed_at: NOW, message_count: 4, ...stamp } as SessionInfo])

      const c = dot(null, 'listed')

      expect(c.querySelector(RUNNING)).not.toBeNull()
      expect(c.querySelector(UNKNOWN)).toBeNull()
    })
  }
})

describe('SessionStatusDot waiting label', () => {
  it('says waiting for a row held only by a fresh waiting card', () => {
    const c = dot({
      execution_observations: [waitingObs],
      id: 'waiting-tree',
      kanban_active: 1,
      kanban_observed_at: NOW,
      kanban_parked: 1,
      message_count: 4
    })

    expect(c.querySelector(WAITING)?.getAttribute('title')).toBe('Open work, waiting')
    expect(c.querySelector(RUNNING)).toBeNull()
  })

  it('says waiting for a listed row whose open cards are all parked', () => {
    const row = {
      execution_observations: [],
      id: 'all-parked',
      kanban_active: 2,
      kanban_observed_at: NOW,
      kanban_parked: 2,
      message_count: 4
    } as Partial<SessionInfo>

    $sessions.set([row as SessionInfo])

    expect(dot(row).querySelector(WAITING)).not.toBeNull()
  })

  // [daveey] PR #2234 review: a held card is red on the stripe, so the label must not say waiting.
  it('keeps running when a held card sits beside the waiting one', () => {
    const c = dot({
      execution_observations: [waitingObs],
      id: 'held',
      kanban_active: 2,
      kanban_held: 1,
      kanban_observed_at: NOW,
      kanban_parked: 1,
      message_count: 4
    })

    expect(c.querySelector(RUNNING)).not.toBeNull()
    expect(c.querySelector(WAITING)).toBeNull()
  })

  it('keeps running when a running card sits beside the waiting one', () => {
    const c = dot({
      execution_observations: [waitingObs, fixture.base],
      id: 'mixed',
      kanban_active: 2,
      kanban_observed_at: NOW,
      kanban_parked: 1,
      message_count: 4
    })

    expect(c.querySelector(RUNNING)).not.toBeNull()
    expect(c.querySelector(WAITING)).toBeNull()
  })
})
