import { act, cleanup, render, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { useSessionDotState } from '@/lib/use-session-dot-state'
import { $backgroundStatusBySession } from '@/store/composer-status'
import { rowExecution } from '@/store/execution-observation-row'
import {
  activeGatewayProfileKey,
  ensureGatewayForProfile,
  gatewayRouteState,
  isActivePrimary,
  setPrimaryGateway
} from '@/store/gateway'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $liveWorkRoster, setLiveWorkRosterSource } from '@/store/live-work-roster'
import { $projectTree } from '@/store/project-tree'
import {
  $cronSessions,
  $messagingSessions,
  $sessions,
  $unlistedSessionOwnerRows,
  _resetSessionOwnerHintsForTests,
  setSessionOwnerHint
} from '@/store/session'
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
  setLiveWorkRosterSource(ownSource(), [
    { background: 0, execution_observations: [fixture.base], session_id: sessionId, subagents: 0 }
  ])

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
  $backgroundStatusBySession.set({})
  $cronSessions.set([])
  $messagingSessions.set([])
  $projectTree.set([])
  $unlistedSessionOwnerRows.set([])
  _resetSessionOwnerHintsForTests({ storage: true })
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

// [daveey] #2287 follow-up (t_01a1d595575df431): each input in `rowExecutionStores()` repaints the dot on its own.
describe('SessionStatusDot owner-route inputs (no tick lag)', () => {
  const unknownRow = (id: string) => ({
    execution_observations: [unknownObs],
    id,
    kanban_active: 1,
    kanban_observed_at: NOW,
    kanban_parked: 1,
    message_count: 4
  })

  const runningCardAt = (source: string, sessionId: string) =>
    setLiveWorkRosterSource(source, [
      { background: 0, execution_observations: [fixture.base], session_id: sessionId, subagents: 0 }
    ])

  // `$tileOwnerBindings` keys on the tile's owner, not only on how many tiles there are.
  it('relabels as soon as an existing tile gains the owner whose roster reports a running card', () => {
    const tick = $kanbanObservationTick.get()

    runningCardAt(gatewayRouteState(null, 'owner-elsewhere').key, 'owned2')
    $sessionTiles.set([{ storedSessionId: 'owned2' }])

    const c = dot(unknownRow('owned2'))

    expect(c.querySelector(UNKNOWN)).not.toBeNull()

    act(() => $sessionTiles.set([{ ownerProfile: 'owner-elsewhere', storedSessionId: 'owned2' }]))

    expect($kanbanObservationTick.get()).toBe(tick)
    expect(c.querySelector(RUNNING)).not.toBeNull()
  })

  // ...and on which stored session the tile holds.
  it('relabels as soon as an owner tile is rebound to the row', () => {
    const tick = $kanbanObservationTick.get()

    runningCardAt(gatewayRouteState(null, 'owner-elsewhere').key, 'owned3')
    $sessionTiles.set([{ ownerProfile: 'owner-elsewhere', storedSessionId: 'someone-else' }])

    const c = dot(unknownRow('owned3'))

    expect(c.querySelector(UNKNOWN)).not.toBeNull()

    act(() => $sessionTiles.set([{ ownerProfile: 'owner-elsewhere', storedSessionId: 'owned3' }]))

    expect($kanbanObservationTick.get()).toBe(tick)
    expect(c.querySelector(RUNNING)).not.toBeNull()
  })

  // `$activeGatewayRoute`: a primary re-home moves the registry first (no store notifies), then its activation
  // publishes the new profile after the epoch bump, so only the route atom's notification sees the new source.
  it('relabels when the active route moves to a profile whose roster reports a running card', () => {
    const tick = $kanbanObservationTick.get()
    const before = activeGatewayProfileKey()
    const c = dot(unknownRow('routed'))

    try {
      setPrimaryGateway(null, 'rehomed-primary')
      const next = gatewayRouteState(null, 'rehomed-primary').key

      expect(next).not.toBe(ownSource())
      act(() => runningCardAt(next, 'routed'))
      expect(c.querySelector(UNKNOWN)).not.toBeNull()

      act(() => void ensureGatewayForProfile('rehomed-primary'))

      expect(activeGatewayProfileKey()).toBe('rehomed-primary')
      expect($kanbanObservationTick.get()).toBe(tick)
      expect(c.querySelector(RUNNING)).not.toBeNull()
    } finally {
      setPrimaryGateway(null, before)
      void ensureGatewayForProfile(before)
    }
  })

  // `$gatewayActivationEpoch`: a same-profile re-activation publishes no new profile, so the epoch is the only store
  // that notifies. An owner hint is module state (no store), so a route activation is where the dot picks it up.
  it('re-evaluates the row on a same-profile route activation', () => {
    const tick = $kanbanObservationTick.get()
    const route = { connectionId: 'local', profile: 'owner-hinted' }
    const c = dot(unknownRow('hinted'))

    act(() => runningCardAt(gatewayRouteState(route.connectionId, route.profile).key, 'hinted'))
    setSessionOwnerHint('hinted', route)
    expect(c.querySelector(UNKNOWN)).not.toBeNull()

    act(() => void ensureGatewayForProfile(activeGatewayProfileKey()))

    expect($kanbanObservationTick.get()).toBe(tick)
    expect(c.querySelector(RUNNING)).not.toBeNull()
  })

  // The owner-lookup rows a runtime's owner falls back to. A running process with no execution evidence counts toward
  // the row only when its runtime resolves to the row's owner source, and that owner is named only by the new row: a
  // later lineage tip (`_lineage_root_id`) whose own id no dot-state store claims. Rendered through the hook alone,
  // because the dot also subscribes `$sessionColorById`, which repaints it on any project-tree change.
  const ownerRowStores: Array<[string, (row: SessionInfo) => void]> = [
    ['$cronSessions', row => $cronSessions.set([row])],
    ['$messagingSessions', row => $messagingSessions.set([row])],
    [
      '$projectTree',
      row =>
        $projectTree.set([
          { id: 'p', label: 'p', path: null, previewSessions: [row], repos: [], sessionCount: 1 } as SidebarProjectTree
        ])
    ],
    ['$unlistedSessionOwnerRows', row => $unlistedSessionOwnerRows.set([row])]
  ]

  for (const [name, publish] of ownerRowStores) {
    it(`re-evaluates the row when ${name} names the owner of a running process on it`, () => {
      const tick = $kanbanObservationTick.get()
      const id = `proc-${name.slice(1)}`
      const owned = { id, kanban_observed_at: NOW, message_count: 4, profile: 'owner-elsewhere' } as SessionInfo

      $backgroundStatusBySession.set({ [id]: [{ id: 'job', state: 'running', title: 'job', type: 'background' }] })

      const { result } = renderHook(() => useSessionDotState(id, owned))

      expect(result.current).toBe('idle')

      act(() =>
        publish({
          _lineage_root_id: id,
          id: `${id}-tip`,
          kanban_observed_at: NOW,
          message_count: 4,
          profile: 'owner-elsewhere'
        } as SessionInfo)
      )

      expect($kanbanObservationTick.get()).toBe(tick)
      expect(result.current).toBe('background')
    })
  }
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

describe('SessionStatusDot external label', () => {
  it('names the service when kanban_external_cards are present', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)

    try {
      const c = dot({
        id: 'ext-dot',
        kanban_active: 1,
        kanban_external: 1,
        kanban_external_cards: [
          {
            external: {
              adapter: 'github_run',
              health: 'ok',
              job: 'running',
              next_due_at: 1300,
              observed_at: 1000,
              reason: "job 'tests', 12 min",
              service: 'GitHub Actions'
            },
            task_id: 't_ext1'
          }
        ],
        kanban_observed_at: 1000,
        kanban_parked: 1,
        message_count: 4
      })

      const el = c.querySelector('[data-background-label="external"]')

      expect(el).not.toBeNull()
      expect(el?.getAttribute('title')).toContain('Running on GitHub Actions')
      act(() => {
        vi.advanceTimersByTime(301_000)
      })
      expect(c.querySelector('[data-background-label="external"]')?.getAttribute('title')).toContain(
        'Status unknown on GitHub Actions'
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('missing cards keep the generic external label, not a service claim', () => {
    const c = dot({
      id: 'ext-generic',
      kanban_active: 1,
      kanban_external: 1,
      kanban_observed_at: Date.now() / 1000,
      kanban_parked: 1,
      message_count: 4
    })

    const el = c.querySelector('[data-background-label="external"]')

    expect(el?.getAttribute('title')).toBe('Running on an external service')
    expect(el?.getAttribute('title')).not.toContain('GitHub')
  })

  it('down-only rows are labelled down, not running on an external service', () => {
    const c = dot({
      id: 'ext-down-only',
      kanban_active: 1,
      kanban_external_down: 1,
      kanban_observed_at: Date.now() / 1000,
      kanban_parked: 1,
      message_count: 4
    })

    const el = c.querySelector('[data-background-label="external_down"]')

    expect(el).not.toBeNull()
    expect(el?.getAttribute('title')).not.toBe('Running on an external service')
    expect(el?.getAttribute('title')?.toLowerCase()).toMatch(/down|unreachable/)
  })
})
