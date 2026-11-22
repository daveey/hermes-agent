import { act, cleanup, render } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneTab } from '@/components/ui/pane-tab'
import { sessionsHost } from '@/sdk/sessions'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { rowExecution } from '@/store/execution-observation-row'
import { setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $profiles } from '@/store/profile'
import {
  $selectedStoredSessionId,
  $sessions,
  _resetSessionOwnerHintsForTests,
  setSessionOwnerHint
} from '@/store/session'
import { $blockedSessionIds } from '@/store/session-blocked'
import { $sessionDotStateById, type SessionDotState } from '@/store/session-dot-state'
import type * as DotStateModule from '@/store/session-dot-state'
import { $unavailableWorkSources } from '@/store/session-work-source'
import { _resetWorkSourceOwnerForTests } from '@/store/work-source-owner'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionInfo } from '@/types/hermes'

import fixture from '../../../../../schemas/fixtures/execution-observations-v1.json'

import { SessionTabAccent } from './session-tab-accent'
import { SessionTabMenu, WorkspaceTabMenu } from './session-tile'

vi.mock('@/store/session-dot-state', async importOriginal => ({
  ...(await importOriginal<typeof DotStateModule>()),
  $sessionDotStateById: (await import('nanostores')).atom<Record<string, SessionDotState>>({})
}))

const states = $sessionDotStateById as WritableAtom<Record<string, SessionDotState>>

const row = {
  id: 'session',
  profile: 'default',
  connection_id: 'local',
  message_count: 2,
  outcome: canonicalOutcome('done'),
  kanban_observed_at: Date.now() / 1000
} as SessionInfo

beforeEach(() => {
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
  setSessionOwnerHint(row.id, { connectionId: 'local', profile: 'default' })

  for (const source of ['lineage', 'tasks', 'process', 'subagent', 'control'] as const) {
    const read = sessionsHost.beginWorkSourceRead({
      connectionId: 'local',
      profile: 'default',
      storedSessionId: row.id,
      source
    })

    sessionsHost.reportWorkSourceRead({ ...read, available: true })
  }
})

function tab(session: SessionInfo | null = row) {
  return (
    <SessionTabAccent session={session} storedSessionId={session?.id ?? null}>
      <PaneTab active>Session</PaneTab>
    </SessionTabAccent>
  )
}

afterEach(() => {
  cleanup()
  _resetWorkSourceOwnerForTests()
  _resetSessionOwnerHintsForTests({ storage: true })
  $unavailableWorkSources.set({})
  setPrimaryGateway(null)
  setPrimaryGatewayConnection(null)
  $connectionsRegistry.set(null)
  $profiles.set([])
  vi.useRealTimers()
  $blockedSessionIds.set([])
  $selectedStoredSessionId.set(null)
  $sessions.set([])
  states.set({})
})

describe('session tab underline', () => {
  it.each([false, true])('does not reclassify published waiting on an observation tick (listed=%s)', async listed => {
    vi.useFakeTimers()
    vi.setSystemTime(1001_000)

    const session = {
      ...row,
      kanban_observed_at: 1001,
      outcome: canonicalOutcome('waiting'),
      execution_observations: [
        {
          ...fixture.base,
          state: 'waiting',
          valid_until: 1010,
          wait: {
            kind: 'dependency',
            owner: fixture.base.owner,
            predicate: 'parent finishes',
            since: 1000,
            target: fixture.base.identity
          }
        }
      ]
    } as SessionInfo

    if (listed) {
      $sessions.set([session])
    }

    const { container } = render(tab(session))
    const stripe = () => container.querySelector('[data-session-tab-accent]')?.getAttribute('data-session-tab-accent')

    expect(stripe()).toBe('waiting')
    await act(async () => {
      vi.setSystemTime(1011_000)
      $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    })
    expect(rowExecution(session)).toMatchObject({ unknown: 1, waiting: 0 })
    expect(stripe()).toBe('waiting')
  })

  it('scopes the accent on the real workspace and tile tab wrappers', () => {
    const { container, rerender } = render(
      <WorkspaceTabMenu>
        <PaneTab active>New session</PaneTab>
      </WorkspaceTabMenu>
    )

    expect(container.querySelector('[data-session-tab-accent="attention"]')).not.toBeNull()

    act(() => {
      $sessions.set([row])
      $selectedStoredSessionId.set(row.id)
    })
    expect(container.querySelector('[data-session-tab-accent="done"]')).not.toBeNull()
    rerender(
      <SessionTabMenu storedSessionId={row.id} tabPaneId={`session-tile:${row.id}`}>
        <PaneTab active>Tile</PaneTab>
      </SessionTabMenu>
    )
    expect(container.querySelector('[data-session-tab-accent="done"]')).not.toBeNull()
    act(() => states.set({ session: 'needs-input' }))
    expect(container.querySelector('[data-session-tab-accent="done"]')).not.toBeNull()
  })

  it.each([
    ['idle', 'done', 'emerald-500'],
    ['unread', 'done', 'emerald-500'],
    ['needs-input', 'done', 'emerald-500'],
    ['stalled', 'done', 'emerald-500'],
    ['working', 'done', 'emerald-500'],
    ['background', 'done', 'emerald-500'],
    ['draft', 'done', 'emerald-500']
  ] as const)('preserves the published done palette despite local %s', (state, stripe, color) => {
    states.set({ session: state })
    // [daveey] A draft has sent nothing: a row with messages overrides a stale `draft` claim (withRowActivity).
    const { container } = render(tab(state === 'draft' ? { ...row, message_count: 0 } : row))
    const accent = container.querySelector('[data-session-tab-accent]')!

    expect(accent.getAttribute('data-session-tab-accent')).toBe(stripe)
    expect(accent.className).toContain(`--color-${color}`)
    expect((accent as HTMLElement).style.getPropertyValue('--pane-tab-active-accent')).toBe(
      'var(--session-status-color)'
    )
  })

  it('reacts only to published revisions, not turn, blocking or row metadata', () => {
    const { container, rerender } = render(tab())
    const stripe = () => container.querySelector('[data-session-tab-accent]')!.getAttribute('data-session-tab-accent')
    expect(stripe()).toBe('done')
    act(() => states.set({ session: 'needs-input' }))
    expect(stripe()).toBe('done')
    act(() => $blockedSessionIds.set(['session']))
    expect(stripe()).toBe('done')
    rerender(tab({ ...row, cron_active: true, outcome: canonicalOutcome('waiting', { revision: 8 }) }))
    expect(stripe()).toBe('waiting')
    rerender(tab({ ...row, kanban_blocked: 1, outcome: canonicalOutcome('stuck', { revision: 9 }) }))
    expect(stripe()).toBe('stuck')
    rerender(tab({ ...row, outcome: undefined }))
    expect(stripe()).toBe('attention')
    rerender(tab(null))
    expect(stripe()).toBe('attention')
    expect(container.firstElementChild!.className).toContain('[--session-status-color:var(--color-amber-500)]')
  })

  it.each([
    { kanban_observed_at: undefined },
    { kanban_unknown: true },
    { kanban_active: 1 },
    { kanban_observed_at: 1 }
  ])('does not mark unverified tree-only rows done: %j', evidence => {
    const { container } = render(tab({ ...row, outcome: undefined, ...evidence }))

    expect(container.querySelector('[data-session-tab-accent="done"]')).toBeNull()
  })

  it('legacy done without a published status remains unknown despite unread authority', () => {
    $unavailableWorkSources.set({})
    const { container } = render(tab({ ...row, outcome: { kind: 'done', source: 'derived', at: 1000 } }))
    expect(container.querySelector('[data-session-tab-accent="attention"]')).not.toBeNull()
  })

  it('does not finish an unread stored tab whose row is missing', () => {
    states.set({ session: 'unread' })

    const { container } = render(
      <SessionTabAccent storedSessionId="session">
        <span>Missing</span>
      </SessionTabAccent>
    )

    expect(container.querySelector('[data-session-tab-accent="done"]')).toBeNull()
  })

  it('leaves non-session tabs on their existing theme accent', () => {
    const { container } = render(<PaneTab active>Files</PaneTab>)

    expect(container.querySelector('[data-session-tab-accent]')).toBeNull()
    expect(container.firstElementChild!.className).toContain('var(--pane-tab-active-accent,var(--theme-primary))')
    expect((container.firstElementChild as HTMLElement).style.getPropertyValue('--pane-tab-active-accent')).toBe('')
  })
})
