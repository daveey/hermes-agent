// [daveey] Plan231363 all-surface parity, on mounted components: the sidebar row stripe, the pane tab accent and
// the status dot of one session all paint the backend envelope's `presentation.stripe`, a pending or failed
// work-source read (refetch, reconnect) never repaints it, and every committed paint is reported with the revision
// it painted. The work-source DATA authority these reads carry is asserted in store/session-work-source*.test.ts.
import { act, cleanup, render, renderHook, screen } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneTab } from '@/components/ui/pane-tab'
import { sessionsHost } from '@/sdk/sessions'
import {
  type CommittedStripeReport,
  resetCommittedStripeReports,
  setCommittedStripeReporter
} from '@/store/committed-stripe-report'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import { $sessions, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $sessionDotStateById, type SessionDotState } from '@/store/session-dot-state'
import type * as DotStateModule from '@/store/session-dot-state'
import { $unavailableWorkSources } from '@/store/session-work-source'
import { _resetWorkSourceOwnerForTests } from '@/store/work-source-owner'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionInfo } from '@/types/hermes'
import type { CanonicalStripe, SessionOutcome } from '@/types/session-outcome'

import { SessionStatusDot } from './session-status-dot'
import { SessionTabAccent } from './session-tab-accent'
import { useSessionRowStripe } from './sidebar/session-row-stripe'

vi.mock('@/store/session-dot-state', async importOriginal => ({
  ...(await importOriginal<typeof DotStateModule>()),
  $sessionDotStateById: (await import('nanostores')).atom<Record<string, SessionDotState>>({})
}))

const states = $sessionDotStateById as WritableAtom<Record<string, SessionDotState>>
const SOURCES = ['lineage', 'tasks', 'process', 'subagent', 'control'] as const
const scope = { connectionId: 'local', profile: 'default', storedSessionId: 'session' }
const reports: CommittedStripeReport[] = []

const sessionRow = (outcome: SessionOutcome, extra: Partial<SessionInfo> = {}) =>
  ({
    id: 'session',
    profile: 'default',
    connection_id: 'local',
    message_count: 2,
    // Fetched inputs that say something else than the envelope: open and blocked cards.
    kanban_active: 2,
    kanban_blocked: 1,
    kanban_observed_at: Date.now() / 1000,
    outcome,
    ...extra
  }) as SessionInfo

/** Mount the three surfaces of one session and read what each one actually painted. */
function mount(session: SessionInfo) {
  $sessions.set([session])

  const tab = render(
    <SessionTabAccent session={session} storedSessionId={session.id}>
      <PaneTab active>Session</PaneTab>
    </SessionTabAccent>
  )

  const dot = render(<SessionStatusDot session={session} storedSessionId={session.id} />)
  const rowStripe = renderHook(() => useSessionRowStripe(session, states.get()[session.id] ?? 'idle', true))

  return {
    dot: () => dot.container.querySelector('span > span:last-child')?.className ?? '',
    row: () => rowStripe.result.current.statusStripe,
    tab: () => tab.container.querySelector('[data-session-tab-accent]')?.getAttribute('data-session-tab-accent'),
    unmount: () => {
      tab.unmount()
      dot.unmount()
      rowStripe.unmount()
    }
  }
}

const settle = () => act(async () => void (await new Promise(resolve => setTimeout(resolve, 5))))
const latest = (surface: string) => reports.filter(report => report.surface === surface).at(-1)

beforeEach(() => {
  reports.length = 0
  resetCommittedStripeReports()
  setCommittedStripeReporter(async (_row, report) => {
    reports.push(report)

    return { ok: true }
  })
  $connectionsRegistry.set({
    primary: 'local',
    activeConnectionId: 'local',
    connections: [{ id: 'local', kind: 'local' }]
  } as never)
  $profiles.set([{ name: 'default' }] as never)
  setPrimaryGateway(
    { connectionState: 'open', onEvent: () => () => undefined, onState: () => () => undefined } as never,
    'default'
  )
  setPrimaryGatewayConnection({ connectionId: 'local' })
  setSessionOwnerHint('session', { connectionId: 'local', profile: 'default' })

  for (const source of SOURCES) {
    sessionsHost.reportWorkSourceRead({ ...sessionsHost.beginWorkSourceRead({ ...scope, source }), available: true })
  }
})

afterEach(() => {
  cleanup()
  setCommittedStripeReporter(null)
  resetCommittedStripeReports()
  _resetWorkSourceOwnerForTests()
  _resetSessionOwnerHintsForTests({ storage: true })
  $unavailableWorkSources.set({})
  setPrimaryGateway(null)
  setPrimaryGatewayConnection(null)
  $connectionsRegistry.set(null)
  $profiles.set([])
  $sessions.set([])
  states.set({})
})

/** No card activity on the row, so the dot shows its unread tone rather than work in flight. */
const quiet = { kanban_blocked: 0, kanban_active: 0 }
const STRIPES: CanonicalStripe[] = ['done', 'waiting', 'attention', 'blocked', 'stuck', 'none']
const tabName = (stripe: CanonicalStripe) => (stripe === 'none' ? 'neutral' : stripe)
const rowName = (stripe: CanonicalStripe) => (stripe === 'none' ? null : stripe)

describe('one canonical status on every mounted surface', () => {
  it.each(STRIPES)('row and tab both paint %s and report it with its revision', async stripe => {
    states.set({ session: 'idle' })
    const surfaces = mount(sessionRow(canonicalOutcome(stripe, { revision: 41 }, { kind: 'done' })))

    await settle()

    expect(surfaces.row()).toBe(rowName(stripe))
    expect(surfaces.tab()).toBe(tabName(stripe))

    for (const surface of ['row', 'tab']) {
      expect(latest(surface)).toMatchObject({ mounted: true, revision: 41, status: 'canonical', stripe })
      expect(latest(surface)).not.toHaveProperty('local_override')
    }

    expect(latest('dot')).toMatchObject({ revision: 41, status: 'canonical', stripe })
    expect(latest('dot')).not.toHaveProperty('local_override')
  })

  it.each(STRIPES)('a refetch, a failed read and a reconnect never repaint %s', async stripe => {
    states.set({ session: 'idle' })
    const surfaces = mount(sessionRow(canonicalOutcome(stripe, { revision: 5 }, { kind: 'done' })))
    const seen = new Set<unknown>()

    const observe = async () => {
      await settle()
      seen.add(`${surfaces.row()}|${surfaces.tab()}`)
    }

    await observe()

    for (let round = 0; round < 3; round++) {
      // A refetch in flight: the read is pending.
      const reads = SOURCES.map(source => sessionsHost.beginWorkSourceRead({ ...scope, source }))

      await observe()
      // The reconnect failed the read.
      act(() => reads.forEach(read => sessionsHost.reportWorkSourceRead({ ...read, available: false })))
      await observe()
      // It recovered.
      act(() =>
        SOURCES.forEach(source =>
          sessionsHost.reportWorkSourceRead({
            ...sessionsHost.beginWorkSourceRead({ ...scope, source }),
            available: true
          })
        )
      )
      await observe()
    }

    expect([...seen]).toEqual([`${rowName(stripe)}|${tabName(stripe)}`])
    // One paint, one report per surface: the repeated reads reported nothing new.
    expect(reports.filter(report => report.surface === 'row')).toHaveLength(1)
    expect(reports.filter(report => report.surface === 'tab')).toHaveLength(1)
  })

  it('a new revision repaints every surface and is reported once', async () => {
    states.set({ session: 'idle' })
    const first = mount(sessionRow(canonicalOutcome('waiting', { revision: 8 })))

    await settle()
    expect([first.row(), first.tab()]).toEqual(['waiting', 'waiting'])
    first.unmount()

    const second = mount(sessionRow(canonicalOutcome('done', { revision: 9 })))

    await settle()
    expect([second.row(), second.tab()]).toEqual(['done', 'done'])
    expect(latest('row')).toMatchObject({ mounted: true, revision: 9, stripe: 'done' })
    expect(latest('tab')).toMatchObject({ mounted: true, revision: 9, stripe: 'done' })
  })

  it('an unsupported status_version is amber unknown on every surface, reported without a revision', async () => {
    states.set({ session: 'unread' })
    const surfaces = mount(sessionRow({ kind: 'done', status: { status_version: 2 } } as SessionOutcome, quiet))

    await settle()

    expect(surfaces.row()).toBe('attention')
    expect(surfaces.tab()).toBe('attention')
    expect(surfaces.dot()).toContain('--color-amber-500')

    for (const surface of ['row', 'tab', 'dot']) {
      expect(latest(surface)).toMatchObject({ revision: null, status: 'unknown', stripe: 'attention' })
    }
  })

  it('the unread dot takes the envelope tone: green for done, grey for waiting, whatever the legacy kind', async () => {
    states.set({ session: 'unread' })
    const done = mount(sessionRow(canonicalOutcome('done', { revision: 3 }, { kind: 'waiting' }), quiet))

    await settle()
    expect(done.dot()).toContain('bg-(--ui-success)')
    expect(latest('dot')).toMatchObject({ revision: 3, stripe: 'done' })
    expect(latest('dot')).not.toHaveProperty('local_override')
    done.unmount()

    const waiting = mount(sessionRow(canonicalOutcome('waiting', { revision: 4 }, { kind: 'done' }), quiet))

    await settle()
    expect(waiting.dot()).toContain('--color-zinc-400')
    expect(latest('dot')).toMatchObject({ revision: 4, stripe: 'waiting' })
  })

  it("this window's live turn does not repaint the canonical envelope", async () => {
    states.set({ session: 'working' })
    const surfaces = mount(sessionRow(canonicalOutcome('done', { revision: 6 })))

    await settle()

    expect(surfaces.row()).toBe('done')
    expect(surfaces.tab()).toBe('done')

    for (const surface of ['row', 'tab', 'dot']) {
      expect(latest(surface)).toMatchObject({ revision: 6, stripe: 'done' })
      expect(latest(surface)).not.toHaveProperty('local_override')
    }
  })

  // Acceptance probes: Plan231363 requires canonical-only paint, not an observed local override.
  it('contract: the settled idle dot paints canonical done like the row and tab', async () => {
    states.set({ session: 'idle' })
    const surfaces = mount(sessionRow(canonicalOutcome('done'), quiet))

    await settle()

    expect([surfaces.row(), surfaces.tab()]).toEqual(['done', 'done'])
    expect(latest('dot')).toMatchObject({ stripe: 'done' })
    expect(latest('dot')).not.toHaveProperty('local_override')
  })

  it('contract: local activity cannot veto the published stripe', async () => {
    states.set({ session: 'working' })
    const surfaces = mount(sessionRow(canonicalOutcome('done')))

    await settle()

    expect([surfaces.row(), surfaces.tab()]).toEqual(['done', 'done'])
  })

  it('contract: a missing status protocol is unknown, never heuristic green', async () => {
    states.set({ session: 'idle' })
    const surfaces = mount(sessionRow({ kind: 'done' }, quiet))

    await settle()

    expect([surfaces.row(), surfaces.tab()]).toEqual(['attention', 'attention'])
  })

  it('a settled canonical status remains accessible even when the local activity is idle', async () => {
    states.set({ session: 'idle' })
    mount(sessionRow(canonicalOutcome('done'), quiet))
    await settle()
    expect(screen.queryByRole('status', { name: 'Done' })).not.toBeNull()
  })

  it('an older backend without the protocol paints and reports unknown on all surfaces', async () => {
    states.set({ session: 'idle' })
    const surfaces = mount(sessionRow({ kind: 'done' }, { kanban_active: 0, kanban_blocked: 0 }))

    await settle()

    expect(surfaces.row()).toBe('attention')
    expect(surfaces.tab()).toBe('attention')

    for (const surface of ['row', 'tab', 'dot']) {
      expect(latest(surface)).toMatchObject({ revision: null, status: 'unknown', stripe: 'attention' })
    }
  })
})
