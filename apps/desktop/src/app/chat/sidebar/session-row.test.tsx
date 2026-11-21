import { KeyboardSensor, PointerSensor, useSensor, useSensors } from '@dnd-kit/core'
import { sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import type { SessionInfo } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import type * as ChatRuntime from '@/lib/chat-runtime'
import { SESSION_ROW_AREAS, type SessionRowSlotProps } from '@/lib/session-row-slots'
import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import type * as Time from '@/lib/time'
import { sessionsHost } from '@/sdk/sessions'
import type * as ComposerStatusStore from '@/store/composer-status'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $sidebarRowMeta } from '@/store/layout'
import { $projectTree } from '@/store/project-tree'
import { $pullRequestsByBranch, branchPrKey } from '@/store/pull-requests'
import { $unreadFinishedSessionIds, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import type * as SessionStore from '@/store/session'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import type * as SessionStatesStore from '@/store/session-states'
import { $unavailableWorkSources } from '@/store/session-work-source'
import type * as WindowsStore from '@/store/windows'
import { _resetWorkSourceOwnerForTests } from '@/store/work-source-owner'

import executionFixture from '../../../../../../schemas/fixtures/execution-observations-v1.json'

import { ReorderableList, useSortableBindings } from './reorderable-list'
import { SidebarSessionRow } from './session-row'

afterEach(() => {
  cleanup()
  _resetWorkSourceOwnerForTests()
  _resetSessionOwnerHintsForTests({ storage: true })
  $unavailableWorkSources.set({})
  setPrimaryGateway(null)
  $connectionsRegistry.set(null)
})

vi.mock('@/i18n', async () => {
  const { enSessionRowStatus } = await import('@/i18n/en_session_row_status')

  return {
    useI18n: () => ({
      t: {
        sidebar: {
          messageCount: (count: number) => `${count} messages`,
          toolCallCount: (count: number) => `${count} tool calls`,
          projects: {
            home: 'Home'
          },
          row: {
            ...enSessionRowStatus,
            ageMin: 'm',
            ageNow: 'now',
            kanbanTasks: (open: number, blocked: number) => `${open} open, ${blocked} blocked`,
            kanbanAttention: (count: number) => `${count} dropped`,
            kanbanStalled: (count: number) => `${count} stalled`,
            outcomeDone: 'Done',
            outcomeDecision: 'Waiting on your decision',
            outcomeMuted: 'Muted',
            outcomeRunning: 'Running',
            outcomeWaiting: 'Waiting',
            outcomeUnresolved: (reason: null | string) => `Unresolved: ${reason}`,
            backgroundProcesses: (count: number) => `${count} background processes`,
            backgroundProcessUnnamed: 'background process',
            stopProcess: 'Stop',
            stoppingProcess: 'Stopping…',
            backgroundRunning: 'Running in background',
            finishedUnread: 'Finished',
            handoffOrigin: (platform: string) => `Started on ${platform}`,
            messageCount: (count: number) => `${count} messages`,
            needsInput: 'Needs input',
            sessionActions: 'Session actions',
            sessionRunning: 'Running',
            todoProgress: 'Tasks completed',
            waitingForAnswer: 'Waiting for answer'
          }
        },
        assistant: {
          thread: {
            today: (time: string) => `Today at ${time}`,
            yesterday: (time: string) => `Yesterday at ${time}`
          }
        }
      }
    })
  }
})

vi.mock('@/app/chat/profile-tag', () => ({ ProfileTag: () => null }))
vi.mock('@/app/chat/session-drag', () => ({ startSessionDrag: vi.fn() }))
// PlatformAvatar is intentionally NOT mocked (do not reintroduce this — see
// #67500, Gille's third pass): it's a forwardRef component that spreads its
// props onto the rendered span, and mocking it with a stand-in that spreads
// props itself only proves the MOCK forwards them, not that the real
// component does. This file exercises the actual production component so a
// regression in its ref/prop forwarding fails here again.
// Only `sessionTitle` is overridden (makeSession fakes a bare `title` the real
// one wouldn't read); the rest of the module is genuine so the arc test can
// build session state with the same factory the app uses.
const sessionTitle = vi.fn((s: SessionInfo) => (s as unknown as { title: string }).title)

vi.mock('@/lib/chat-runtime', async importOriginal => {
  const actual = await importOriginal<typeof ChatRuntime>()

  return { ...actual, sessionTitle: (s: SessionInfo) => sessionTitle(s) }
})
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/lib/session-source', () => ({
  handoffOriginSource: (state?: string, platform?: string) => (state && platform ? platform : null),
  sessionSourceLabel: (source: string) => source
}))
vi.mock('@/lib/time', async importOriginal => {
  const actual = await importOriginal<typeof Time>()

  return { ...actual, coarseElapsed: () => ({ unit: 'minute' as const, value: 5 }) }
})

// These mocks use importOriginal rather than replacing the module wholesale:
// session-row.tsx (and its transitive imports, e.g. session-color.ts) reads
// several store exports beyond the ones this file cares about, and that set
// keeps growing as the app evolves upstream. A wholesale replacement mock
// silently turns every export it doesn't list into `undefined`, which then
// crashes nanostores' `computed()` the moment a new dependency is added
// upstream (as happened twice already: $stalledSessionIds, then $sessions).
// Overriding only the named atoms we actually control keeps this test
// resilient to that drift.
vi.mock('@/store/composer-status', async importOriginal => {
  const actual = await importOriginal<typeof ComposerStatusStore>()

  return { ...actual, $backgroundRunningSessionIds: atom<string[]>([]) }
})
vi.mock('@/store/session', async importOriginal => {
  const actual = await importOriginal<typeof SessionStore>()

  return { ...actual, $unreadFinishedSessionIds: atom<string[]>([]) }
})
vi.mock('@/store/session-states', async importOriginal => {
  const actual = await importOriginal<typeof SessionStatesStore>()

  return {
    ...actual,
    $attentionSessionIds: atom<string[]>([]),
    $stalledSessionIds: atom<string[]>([]),
    openSessionTile: vi.fn()
  }
})
vi.mock('@/store/windows', async importOriginal => {
  const actual = await importOriginal<typeof WindowsStore>()

  return {
    ...actual,
    canOpenSessionWindow: () => false,
    openSessionInNewWindow: vi.fn()
  }
})

// SessionActionsMenu open behavior is covered in session-actions-menu.test.tsx
// against the real component. Stub it here so this file stays focused on the
// row chrome (handoff avatar tip, etc.) — but record the props so the row's
// own state plumbing (e.g. the archived flag, #98813) is still asserted.
const menuProps = vi.hoisted(() => vi.fn())

vi.mock('./session-actions-menu', () => ({
  SessionActionsMenu: (props: { children?: React.ReactNode }) => {
    menuProps(props)

    return <>{props.children}</>
  },
  SessionContextMenu: (props: { children?: React.ReactNode }) => {
    menuProps(props)

    return <>{props.children}</>
  }
}))

vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm: vi.fn(), notePointerMove: vi.fn(), startPrewarm: vi.fn() })
}))

function makeSession(overrides: Partial<SessionInfo> & { title: string }): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id: 's1',
    kanban_observed_at: Date.now() / 1000,
    last_active: 0,
    profile: 'default',
    started_at: 0,
    ...overrides
  } as unknown as SessionInfo
}

const noop = vi.fn()

// Existing chrome tests assume authoritative source reads; establish them through
// the real SDK/owner/transport seam, not by suppressing the mounted hook.
function certifyRow(session: SessionInfo, reads = true): void {
  const connectionId = session.connection_id || 'local'
  const profile = session.profile || 'default'
  $connectionsRegistry.set({
    activeConnectionId: connectionId,
    primary: connectionId,
    connections: [{ id: connectionId, kind: 'local', label: 'Fixture' }]
  } as never)
  setPrimaryGateway(
    {
      connectionState: 'open',
      onEvent: () => () => undefined,
      onState: () => () => undefined,
      request: vi.fn()
    } as never,
    profile
  )
  setPrimaryGatewayConnection({ connectionId })
  setSessionOwnerHint(session.id, { connectionId, profile })

  if (!reads) {
    return
  }

  for (const source of ['lineage', 'tasks', 'process', 'subagent', 'control'] as const) {
    const token = sessionsHost.beginWorkSourceRead({ connectionId, profile, storedSessionId: session.id, source })
    sessionsHost.reportWorkSourceRead({ ...token, available: true })
  }
}

const renderRow = (
  session: SessionInfo,
  extra?: { card?: boolean; onResume?: () => void; selected?: boolean; unreadAuthority?: boolean }
) => {
  if (!extra?.unreadAuthority) {
    certifyRow(session)
  }

  return render(
    <SidebarSessionRow
      card={extra?.card}
      isPinned={false}
      isSelected={extra?.selected ?? false}
      onArchive={noop}
      onDelete={noop}
      onPin={noop}
      onResume={extra?.onResume ?? noop}
      onToggleUnread={noop}
      session={session}
      unread={false}
    />
  )
}

it.each([['object', { a: 1 }], ['number', 7], ['boolean', true]] as const)(
  'keeps rendering the sidebar row with non-array %s observations',
  (_kind, value) => {
    const { container } = renderRow(makeSession({
      title: 'Malformed observations',
      kanban_active: 1,
      execution_observations: value as unknown as SessionInfo['execution_observations']
    }))

    expect(screen.getByText('Malformed observations')).not.toBeNull()
    expect(container.querySelector('[data-run-square]')?.getAttribute('data-run-square')).toBe('running')
    expect(container.querySelector('[data-card-colour="green"]')).toBeNull()
  }
)

describe('always-on left run square', () => {
  for (const card of [false, true]) {
    for (const kanban_active of [0, 2]) {
      it(`sits after the dot lead and before the title (card=${card}, active=${kanban_active})`, () => {
        const now = Date.now() / 1000

        const execution_observations = Array.from({ length: kanban_active }, (_, i) => ({
          ...executionFixture.base,
          identity: { ...executionFixture.base.identity, work_id: `task-${i}` },
          observed_at: now,
          valid_until: now + 180,
          last_activity: now
        }))

        const { container } = renderRow(makeSession({ title: 'Run markers', kanban_active, execution_observations }), {
          card
        })

        const square = container.querySelector('[data-run-square]')!
        expect(square).not.toBeNull()
        expect(square.getAttribute('data-run-square')).toBe(kanban_active ? 'running' : 'idle')
        expect(square.className.includes('bg-sky-400')).toBe(kanban_active > 0)
        const lead = square.previousElementSibling!
        expect(lead.querySelector('.rounded-full')).not.toBeNull()
        expect(lead.nextElementSibling).toBe(square)
        const title = screen.getByText('Run markers')
        expect(square.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
        expect(container.querySelector('[data-card-colour="blue"]')).toBeNull()
        expect(screen.queryByTestId('session-row-card-squares')).toBeNull()
      })
    }
  }
})

describe('idle sidebar authoritative source failures', () => {
  afterEach(() => $unavailableWorkSources.set({}))

  // Process / subagent / control are read only for the open chat, so every other row has no reads at all. Treating
  // that as unknown painted every chat but the focused one grey, and green only once navigated to.
  it('paints a stored done row green when its sources were never read (an unfocused chat)', () => {
    const session = makeSession({
      title: 'Unread authority',
      message_count: 2,
      outcome: { kind: 'done', source: 'derived', at: 1000 }
    })

    certifyRow(session, false)
    const { container } = renderRow(session, { unreadAuthority: true })
    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('done')
  })

  it('holds a stored done row off green while a read of its current authority is pending', () => {
    const session = makeSession({
      title: 'Pending authority',
      message_count: 2,
      outcome: { kind: 'done', source: 'derived', at: 1000 }
    })

    certifyRow(session, false)
    act(() => {
      sessionsHost.beginWorkSourceRead({
        connectionId: 'local',
        profile: 'default',
        storedSessionId: 's1',
        source: 'process'
      })
    })
    const { container } = renderRow(session, { unreadAuthority: true })
    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('waiting')
  })

  it.each(['tasks', 'process', 'subagent'] as const)(
    'repaints an idle stored-done row after a real %s read failure, and recovers',
    source => {
      const session = makeSession({
        title: 'Completed',
        connection_id: 'edge',
        message_count: 2,
        outcome: { kind: 'done', source: 'derived', at: 1000 }
      })

      const { container } = renderRow(session)
      const row = () => container.querySelector('[data-sidebar-session-id="s1"]')
      expect(row()?.getAttribute('data-sidebar-stripe')).toBe('done')
      let read!: ReturnType<typeof sessionsHost.beginWorkSourceRead>
      act(() => {
        read = sessionsHost.beginWorkSourceRead({
          connectionId: 'edge',
          profile: 'default',
          storedSessionId: 's1',
          source
        })
        sessionsHost.reportWorkSourceRead({ ...read, available: false })
      })
      expect(row()?.getAttribute('data-sidebar-stripe')).toBe('waiting')
      expect(container.querySelector('[data-slot="session-status-stripe"]')?.getAttribute('data-outcome')).toBeNull()
      act(() => sessionsHost.reportWorkSourceRead({ ...read, connectionId: 'other', available: true }))
      expect(row()?.getAttribute('data-sidebar-stripe')).toBe('waiting')
      act(() => sessionsHost.reportWorkSourceRead({ ...read, available: true }))
      expect(row()?.getAttribute('data-sidebar-stripe')).toBe('done')
      expect(session.outcome?.kind).toBe('done')
    }
  )
})

// The arc marks the SELECTED row — the one the pane shows — not a working
// one. Working state stays on the status dot; the sidebar's busiest rows used
// to wear the arc and the current task got lost among them.
describe('SidebarSessionRow selection arc', () => {
  afterEach(() => {
    clearAllSessionStates()
  })

  const arc = (container: HTMLElement) => container.querySelector('.arc-row')

  it('paints no arc on an unselected settled session', () => {
    const { container } = renderRow(makeSession({ title: 'Settled' }))

    expect(arc(container)).toBeNull()
  })

  it('paints no arc on an unselected running session', () => {
    publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

    const { container } = renderRow(makeSession({ title: 'Running' }))

    expect(arc(container)).toBeNull()
  })

  it('paints the arc on the selected session, idle or not', () => {
    const { container } = renderRow(makeSession({ title: 'Current' }), { selected: true })

    expect(arc(container)).toBeTruthy()
  })
})

describe('SidebarSessionRow', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  // Full-title tooltip on hover (#83000-class ask): the label is a tooltip
  // trigger, but the tip only opens when the title is actually truncated.
  describe('full-title overflow tooltip', () => {
    afterEach(() => {
      vi.useRealTimers()
    })

    const title = 'A very long session title that the sidebar cannot possibly fit'

    /** The rendered title label (tooltip trigger is the label itself). */
    const label = () => screen.getByText(title).closest('[data-slot="tooltip-trigger"]') as HTMLElement

    const setWidths = (el: HTMLElement, scrollWidth: number, clientWidth: number) => {
      Object.defineProperty(el, 'scrollWidth', { configurable: true, value: scrollWidth })
      Object.defineProperty(el, 'clientWidth', { configurable: true, value: clientWidth })
    }

    it('opens with the full title after a settled hover when the title overflows', () => {
      vi.useFakeTimers()
      renderRow(makeSession({ title }))

      const el = label()
      setWidths(el, 300, 100)

      act(() => {
        fireEvent.pointerEnter(el)
        vi.advanceTimersByTime(700)
      })

      expect(screen.getByRole('tooltip').textContent).toContain(title)
    })

    it('stays closed when the title fits', () => {
      vi.useFakeTimers()
      renderRow(makeSession({ title }))

      const el = label()
      setWidths(el, 100, 100)

      act(() => {
        fireEvent.pointerEnter(el)
        vi.advanceTimersByTime(700)
      })

      expect(screen.queryByRole('tooltip')).toBeNull()
    })

    it('cancels a pending open when the pointer leaves before the delay', () => {
      vi.useFakeTimers()
      renderRow(makeSession({ title }))

      const el = label()
      setWidths(el, 300, 100)

      act(() => {
        fireEvent.pointerEnter(el)
        vi.advanceTimersByTime(200)
        fireEvent.pointerLeave(el)
        vi.advanceTimersByTime(700)
      })

      expect(screen.queryByRole('tooltip')).toBeNull()
    })
  })

  // The Archived view reuses the row menu, and the menu needs the row's
  // archived state to label its shared verb Unarchive (#98813).
  it('forwards the archived state to the row menu', () => {
    menuProps.mockClear()
    renderRow(makeSession({ archived: true, title: 'Archived row' }))

    expect(menuProps).toHaveBeenCalledWith(expect.objectContaining({ archived: true }))
  })

  it('forwards the non-archived state to the row menu', () => {
    menuProps.mockClear()
    renderRow(makeSession({ title: 'Live row' }))

    expect(menuProps).toHaveBeenCalledWith(expect.objectContaining({ archived: false }))
  })
})

describe('SidebarSessionRow approved flat trailing slot', () => {
  it('ignores legacy age/token/cost preferences and keeps only squares then the gateway', () => {
    const before = $sidebarRowMeta.get()
    $sidebarRowMeta.set(['updated', 'tokens', 'cost'])

    const row = makeSession({
      title: 'Squares',
      kanban_blocked: 2,
      input_tokens: 12000,
      output_tokens: 500,
      estimated_cost_usd: 1.25,
      background_active: 1,
      background_processes: [{ id: 'p', command: 'sleep 600' }]
    })

    certifyRow(row)
    $connectionsRegistry.set({
      activeConnectionId: 'local',
      primary: 'local',
      connections: [
        { id: 'local', kind: 'local', label: 'Laptop' },
        { id: 'remote', kind: 'ssh', label: 'Cloud' }
      ]
    } as never)
    const { container } = renderRow(row, { unreadAuthority: true })
    expect(container.querySelector('time')).toBeNull()
    expect(screen.queryByTestId('session-row-kanban-counts')).toBeNull()
    expect(screen.queryByTestId('session-row-background-processes')).toBeNull()
    const squares = screen.getByTestId('session-row-card-squares')
    const gateway = container.querySelector('[data-slot="session-gateway-device"]')!
    expect(gateway).not.toBeNull()
    expect(squares.nextElementSibling).toBe(gateway)
    expect(gateway.nextElementSibling).toBeNull()
    expect(container.textContent).not.toContain('12.5k')
    expect(container.textContent).not.toContain('$1.25')
    $sidebarRowMeta.set(before)
  })

  it('leaves the card-mode age and badges unchanged', () => {
    const { container } = renderRow(makeSession({ title: 'Card', kanban_blocked: 1 }), { card: true })
    expect(container.querySelector('time')).not.toBeNull()
    expect(screen.getByTestId('session-row-kanban-counts')).toBeTruthy()
    expect(screen.queryByTestId('session-row-card-squares')).toBeNull()
  })
})

describe('SidebarSessionRow keyboard stops', () => {
  const rowMeta = $sidebarRowMeta.get()
  const pullRequests = $pullRequestsByBranch.get()

  afterEach(() => {
    $sidebarRowMeta.set(rowMeta)
    $pullRequestsByBranch.set(pullRequests)
  })

  it.each([false, true])('keeps timestamp metadata out of keyboard focus (card=%s)', card => {
    $sidebarRowMeta.set(['updated'])
    const { container } = renderRow(makeSession({ title: 'Keyboard session' }), { card })
    const primary = screen.getByRole('button', { name: 'Keyboard session' })
    const actions = screen.getByRole('button', { name: 'Session actions' })
    const timestamp = container.querySelector('time')!

    primary.focus()

    if (card) {
      expect(timestamp).not.toBeNull()
      timestamp.focus()
    } else {
      expect(timestamp).toBeNull()
    }

    expect(globalThis.document.activeElement).toBe(primary)

    const stops = [...container.querySelectorAll<HTMLElement>('button, [tabindex]')].filter(
      element => element.tabIndex >= 0
    )

    expect(stops).toEqual(card ? [actions, primary] : [primary, actions])
    actions.focus()
    expect(globalThis.document.activeElement).toBe(actions)
  })

  it('keeps a slot-ending PR chip visible beside the hover cluster, not under it', () => {
    $sidebarRowMeta.set(['pr'])
    $pullRequestsByBranch.set({
      [branchPrKey('/repo', 'feature')]: {
        branch: 'feature',
        draft: false,
        number: 12345,
        state: 'open',
        title: 'Feature',
        url: 'https://github.com/example/repo/pull/12345'
      }
    })

    renderRow(makeSession({ git_branch: 'feature', git_repo_root: '/repo', title: 'Feature session' }))

    const pullRequest = screen.getByRole('button', { name: 'Open pull request #12345' })
    const cluster = screen.getByRole('button', { name: 'Session actions' }).closest('span.flex')!

    expect(cluster.className).not.toContain('absolute')
    expect(cluster.className).not.toContain('session-row-kebab')
    expect(pullRequest.closest('.session-row-tail')).toBeNull()
    expect(pullRequest.compareDocumentPosition(cluster) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('lifts the hover cluster over a hiding tail, sized to its buttons', () => {
    $sidebarRowMeta.set(['updated'])

    const { container, unmount } = renderRow(makeSession({ title: 'Plain', kanban_unseen: 1 }))
    const cluster = screen.getByRole('button', { name: 'Session actions' }).closest('span.flex')!

    expect(cluster.className).toContain('absolute')
    expect(container.querySelector('.session-row-tail')?.className).toContain('min-w-11')
    unmount()

    const archived = renderRow(makeSession({ archived: true, title: 'Old', kanban_unseen: 1 }))

    expect(archived.container.querySelector('.session-row-tail')?.className).toContain('min-w-5')
  })

  it('retains the real pull-request action as a separate keyboard stop', () => {
    $sidebarRowMeta.set(['pr'])
    $pullRequestsByBranch.set({
      [branchPrKey('/repo', 'feature')]: {
        branch: 'feature',
        draft: false,
        number: 7,
        state: 'open',
        title: 'Feature',
        url: 'https://github.com/example/repo/pull/7'
      }
    })

    const { container } = renderRow(
      makeSession({ git_branch: 'feature', git_repo_root: '/repo', title: 'Feature session' })
    )

    const primary = screen.getByRole('button', { name: 'Feature session' })
    const pullRequest = screen.getByRole('button', { name: 'Open pull request #7' })
    const actions = screen.getByRole('button', { name: 'Session actions' })

    const stops = [...container.querySelectorAll<HTMLElement>('button, [tabindex]')].filter(
      element => element.tabIndex >= 0
    )

    expect(stops).toEqual([primary, pullRequest, actions])
    pullRequest.focus()
    expect(globalThis.document.activeElement).toBe(pullRequest)
    noop.mockClear()
    fireEvent.click(pullRequest)
    expect(noop).not.toHaveBeenCalled()
  })
})

// Regression for #83617: the row shell once spread the FULL dnd-kit handle, so
// Space on a focused control inside the row (the ⋯ button that opens Rename)
// reached the KeyboardSensor's activator — a drag armed, and the sensor then
// ate the next Space at window level (the rename input dropped the keystroke).
describe('SidebarSessionRow inside the sortable list', () => {
  function SortableRow({ onResume, session }: { onResume: () => void; session: SessionInfo }) {
    const { dragHandleProps, dragging, ref, reorderable, style } = useSortableBindings(session.id)

    return (
      <SidebarSessionRow
        dragging={dragging}
        dragHandleProps={dragHandleProps}
        isPinned={false}
        isSelected={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={onResume}
        onToggleUnread={noop}
        ref={ref}
        reorderable={reorderable}
        session={session}
        style={style}
        unread={false}
      />
    )
  }

  function Host({ onResume, session }: { onResume: () => void; session: SessionInfo }) {
    // The sidebar's own sensor set (index.tsx dndSensors).
    const sensors = useSensors(
      useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
      useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
    )

    return (
      <ReorderableList ids={[session.id]} onReorder={noop} sensors={sensors}>
        <SortableRow onResume={onResume} session={session} />
      </ReorderableList>
    )
  }

  const space = { code: 'Space', key: ' ' }

  it('lets Space through to a focused row control instead of arming a keyboard drag', () => {
    const { container } = render(<Host onResume={noop} session={makeSession({ title: 'Renamable' })} />)
    const kebab = screen.getByRole('button', { name: 'Session actions' })
    kebab.focus()

    // Not defaultPrevented (the ⋯ menu is stubbed in this file, so only
    // dnd-kit could have claimed the key) and no grabber reports a drag.
    expect(fireEvent.keyDown(kebab, space)).toBe(true)
    expect(container.querySelector('[aria-pressed="true"]')).toBeNull()
  })

  it('still starts a keyboard reorder from the grabber', () => {
    const { container } = render(<Host onResume={noop} session={makeSession({ title: 'Renamable' })} />)
    const grabber = container.querySelector<HTMLElement>('[data-reorder-handle]')!

    grabber.focus()
    fireEvent.keyDown(grabber, space)
    expect(grabber.getAttribute('aria-pressed')).toBe('true')
  })

  // #38072 finding 3 (axe nested-interactive): the grabber (dnd-kit
  // role="button" + tabIndex) must be a SIBLING of the row's primary action,
  // never a descendant of it. The row body is a div carrying the gesture
  // handlers; the title is the row's real button and its click bubbles to
  // the body's resolver, so pointer users keep click-anywhere-on-the-row.
  it('renders the grabber outside any button, with the title as the row button', () => {
    const onResume = vi.fn()
    const { container } = render(<Host onResume={onResume} session={makeSession({ title: 'Renamable' })} />)

    const grabber = container.querySelector<HTMLElement>('[data-reorder-handle]')!

    // Handle semantics survive (keyboard reorder above depends on them)…
    expect(grabber.getAttribute('role')).toBe('button')
    expect(grabber.tabIndex).toBe(0)
    // …but it no longer nests inside the row's primary button.
    expect(grabber.closest('button')).toBeNull()

    // The title line is the row button; clicks on it resume via the body
    // div's bubbled resolver (no onClick of its own).
    const title = screen.getByRole('button', { name: 'Renamable' })
    expect(title.closest('[data-reorder-handle]')).toBeNull()
    fireEvent.click(title)
    expect(onResume).toHaveBeenCalledTimes(1)
  })
})

// Row-decoration slots: a plugin decorates rows through the registry with the
// row's stored session id handed to its render — the seam the session-list API
// pairs with (see #116305 item 3).
describe('SidebarSessionRow decoration slots', () => {
  const disposers: Array<() => void> = []

  afterEach(() => {
    disposers.splice(0).forEach(dispose => dispose())
  })

  const decorate = (area: string, id: string, testId: string) =>
    disposers.push(
      registry.register({
        area,
        data: {
          render: ({ sessionId }: SessionRowSlotProps) => <span data-testid={testId}>{sessionId}</span>
        },
        id,
        source: 'disk'
      })
    )

  it('mounts leading and trailing decorations, each handed the DURABLE row id', () => {
    act(() => {
      decorate(SESSION_ROW_AREAS.leading, 'deco-lead', 'lead-deco')
      decorate(SESSION_ROW_AREAS.trailing, 'deco-tail', 'tail-deco')
    })

    // Auto-compression rotates the live id. A plugin that remembered the live
    // one decorates this row until the next compaction and then silently stops
    // matching — so the slot hands the lineage root, the id core's own
    // pin/reorder and `host.sessions.*` address.
    renderRow(makeSession({ _lineage_root_id: 'root-9', id: 'live-9', title: 'Compressed' }))

    expect(screen.getByTestId('lead-deco').textContent).toBe('root-9')
    expect(screen.getByTestId('tail-deco').textContent).toBe('root-9')
  })

  it('renders nothing for an area with no registrations and survives an unmount', () => {
    const { container } = renderRow(makeSession({ id: 'row-7', title: 'Plain' }))

    expect(container.querySelector('[data-testid="lead-deco"]')).toBeNull()

    act(() => {
      decorate(SESSION_ROW_AREAS.leading, 'deco-lead', 'lead-deco')
    })

    // Same row, contribution arriving late: the slot mounts it in place.
    expect(screen.getByTestId('lead-deco').textContent).toBe('row-7')

    act(() => {
      disposers.splice(0).forEach(dispose => dispose())
    })

    expect(screen.queryByTestId('lead-deco')).toBeNull()
  })
})

// The card header names the row's project. A backend-Home-owned row files
// under the Home bucket, so the card names Home in human copy, never the
// bucket's internal id.
describe('SidebarSessionRow card project label', () => {
  afterEach(() => {
    $projectTree.set([])
  })

  it('names Home for a Home-owned row with a cwd, not the internal sentinel', () => {
    $projectTree.set([
      {
        id: '__no_project__',
        isNoProject: true,
        label: 'Home',
        path: null,
        repos: [],
        sessionCount: 1,
        sessionIds: ['home-row']
      }
    ])

    const { container } = renderRow(makeSession({ cwd: '/Users/daveey', id: 'home-row', title: 'Fresh home chat' }), {
      card: true
    })

    expect(container.textContent).not.toContain('__no_project__')
    expect(screen.getByText('Home')).toBeTruthy()
  })

  it('keeps naming an unowned cwd row by its folder', () => {
    renderRow(makeSession({ cwd: '/work/scratch', id: 'loose-row', title: 'Loose' }), { card: true })

    expect(screen.getByText('scratch')).toBeTruthy()
  })
})

// [daveey] A session waiting on kanban work shows its open and blocked task
// counts after the age (and before the gateway glyph).
describe('SidebarSessionRow kanban counts', () => {
  it('names the forwarded blocked owner rather than its scheduled relay in the red reason', () => {
    const { container } = renderRow(
      makeSession({
        kanban_active: 1,
        kanban_parked: 1,
        kanban_blocked: 1,
        kanban_blocked_on: ['remote/default/t_owner'],
        title: 'Forwarded'
      }),
      { card: true }
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('stuck')
    expect(screen.getByTestId('session-row-kanban-counts').getAttribute('aria-label')).toBe(
      '1 open, 1 blocked; remote/default/t_owner'
    )
  })

  it('does not show a red reason for a scheduled relay with a nonblocked remote owner', () => {
    const { container } = renderRow(
      makeSession({
        kanban_active: 2,
        kanban_parked: 2,
        kanban_blocked: 0,
        kanban_blocked_on: ['remote/default/t_old'],
        title: 'Scheduled'
      })
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).not.toBe('stuck')
    expect(screen.getByTestId('session-row-card-squares').getAttribute('aria-label')).toBe('Waiting 2')
  })

  afterEach(() => vi.useRealTimers())
  it('shows time-only expiry even when the row stays background and its props do not change', async () => {
    const { $kanbanObservationTick } = await import('@/store/kanban-observation')
    const fixture = await import('../../../../../../schemas/fixtures/execution-observations-v1.json')
    vi.useFakeTimers()
    vi.setSystemTime(1001_000)

    const { container } = renderRow(
      makeSession({
        execution_observations: [{ ...fixture.base, valid_until: 1010 }],
        kanban_active: 1,
        kanban_parked: 1,
        message_count: 2,
        title: 'Expiry'
      }),
      { card: true }
    )

    expect(container.querySelector('[data-kanban-count="unknown"]')).toBeNull()
    expect(container.querySelector('[data-sidebar-stripe]')).toBeNull()
    await act(async () => {
      vi.setSystemTime(1011_000)
      $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
    })
    expect(container.querySelector('[data-kanban-count="unknown"]')?.textContent).toBe('1')
    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('waiting')
  })

  it('explains unknown work in the accessible label and tooltip without implying a new poll', () => {
    vi.useFakeTimers()
    renderRow(makeSession({ execution_observations: [{ source: 'unavailable.work' }], title: 'Unknown' }), {
      card: true
    })
    const counts = screen.getByTestId('session-row-kanban-counts')

    const explanation =
      '1 work item has unknown current status: evidence is missing, expired or unverified. This does not mean done; no new check was requested.'

    expect(counts.querySelector('[data-kanban-count="unknown"]')?.textContent).toBe('1')
    expect(counts.getAttribute('aria-label')).toBe(explanation)
    act(() => {
      fireEvent.pointerMove(counts, { pointerType: 'mouse' })
      vi.advanceTimersByTime(700)
    })
    expect(screen.getByRole('tooltip').textContent).toBe(explanation)
    vi.useRealTimers()
  })

  it('shows open and blocked counts right after the age', () => {
    const { container } = renderRow(
      makeSession({
        execution_observations: [],
        kanban_active: 3,
        kanban_blocked: 1,
        title: 'Waiting'
      } as Partial<SessionInfo> & {
        title: string
      }),
      { card: true }
    )

    const counts = screen.getByTestId('session-row-kanban-counts')
    expect(counts.getAttribute('aria-label')).toBe('3 open, 1 blocked')
    expect(counts.querySelector('[data-kanban-count="open"]')?.textContent).toBe('3')
    expect(counts.querySelector('[data-kanban-count="blocked"]')?.textContent).toBe('1')

    const time = container.querySelector('time')!
    // The counts follow the age in document order.
    expect(time.compareDocumentPosition(counts) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('labels live cards from an older producer without observations as unknown, not certain work', () => {
    renderRow(
      makeSession({ kanban_active: 3, kanban_blocked: 1, title: 'Legacy' } as Partial<SessionInfo> & { title: string }),
      { card: true }
    )

    const label = screen.getByTestId('session-row-kanban-counts').getAttribute('aria-label') ?? ''
    expect(label).toContain('3 work items have unknown current status')
    expect(label).toContain('3 open, 1 blocked')
  })

  it('shows blocked-only work and hides the zero side', () => {
    renderRow(makeSession({ kanban_blocked: 2, title: 'Stuck' } as Partial<SessionInfo> & { title: string }))

    const squares = screen.getByTestId('session-row-card-squares')
    expect(squares.querySelector('[data-card-colour="blue"]')).toBeNull()
    expect(squares.getAttribute('aria-label')).toBe('Needs you 2')
  })

  it('paints the red (stuck) stripe while a kanban card it created is blocked', () => {
    const { container } = renderRow(
      makeSession({ kanban_blocked: 1, message_count: 4, title: 'Blocked' } as Partial<SessionInfo> & {
        title: string
      })
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('stuck')
  })

  // [daveey] A done card whose handoff admits dropped work: amber, not green.
  it('paints the amber (attention) stripe and a warning count for a false-done card', () => {
    const { container } = renderRow(
      makeSession({ kanban_attention: 1, message_count: 4, title: 'Looks done' } as Partial<SessionInfo> & {
        title: string
      })
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('attention')
    const squares = screen.getByTestId('session-row-card-squares')
    expect(squares.querySelector('[data-card-colour="amber"]')).not.toBeNull()
    expect(squares.getAttribute('aria-label')).toBe('Needs attention 1')
  })

  // [daveey] A parked card nothing has produced for over 2 h: an amber stalled count in BOTH trailing branches
  // (the card's figures/age tail and the one-line row's default branch), and the amber stalled glyph.
  it('shows the amber stalled count after the age on a card row', () => {
    const { container } = renderRow(
      makeSession({
        kanban_active: 2,
        kanban_parked: 2,
        kanban_stalled: 1,
        title: 'Stalled'
      } as Partial<SessionInfo> & {
        title: string
      }),
      { card: true }
    )

    const counts = screen.getByTestId('session-row-kanban-counts')
    expect(counts.querySelector('[data-kanban-count="stalled"]')?.textContent).toBe('1')
    expect(counts.querySelector('[data-kanban-count="stalled"]')?.className).toContain('text-amber-500')
    expect(counts.getAttribute('aria-label')).toContain('stalled')
    const time = container.querySelector('time')!
    expect(time.compareDocumentPosition(counts) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows a green worker-wait square and the amber stalled glyph on a one-line row (no age)', async () => {
    const { $sidebarRowMeta } = await import('@/store/layout')
    const before = $sidebarRowMeta.get()
    act(() => $sidebarRowMeta.set([]))

    try {
      const { container } = renderRow(
        makeSession({
          kanban_active: 1,
          kanban_observed_at: Date.now() / 1000,
          kanban_parked: 1,
          kanban_stalled: 1,
          message_count: 4,
          title: 'Stalled one-line'
        } as Partial<SessionInfo> & { title: string })
      )

      expect(container.querySelector('time')).toBeNull()
      const squares = screen.getByTestId('session-row-card-squares')
      expect(squares.querySelector('[data-card-colour="green"]')).not.toBeNull()
      expect(squares.getAttribute('aria-label')).toBe('Waiting for a worker 1')
      expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('stalled')
      const glyph = container.querySelector('[data-slot="session-status-stripe"]')
      expect(glyph?.getAttribute('data-status-stripe')).toBe('stalled')
      expect(glyph?.className).toContain('amber')
      expect(glyph?.querySelector('.codicon-project')).not.toBeNull()
    } finally {
      act(() => $sidebarRowMeta.set(before))
    }
  })

  it('a parked row without stalled cards stays grey waiting', () => {
    const { container } = renderRow(
      makeSession({
        kanban_active: 1,
        kanban_observed_at: Date.now() / 1000,
        kanban_parked: 1,
        message_count: 4,
        title: 'Waiting'
      } as Partial<SessionInfo> & { title: string })
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('waiting')
    expect(screen.getByTestId('session-row-card-squares').querySelector('[data-card-colour="amber"]')).toBeNull()
  })

  it('stays green (done) without kanban_attention', () => {
    const { container } = renderRow(makeSession({ message_count: 4, title: 'Done' }))

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('done')
  })

  it('renders nothing when the session has no kanban work', () => {
    renderRow(makeSession({ title: 'Idle' }))

    expect(screen.queryByTestId('session-row-kanban-counts')).toBeNull()
    expect(screen.queryByTestId('session-row-card-squares')).toBeNull()
  })
})

// [daveey] A project-mode row older than the recents page is not in $sessions,
// so the dot-state store can't see its activity stamps; the row reads its own.
describe('SidebarSessionRow own activity stamps', () => {
  const stripe = (c: HTMLElement) =>
    c.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe') ?? null

  for (const stamp of [{ goal_active: true }, { background_active: 1 }]) {
    it(`keeps the row off green while ${Object.keys(stamp)[0]} is set`, () => {
      const { container } = renderRow(
        makeSession({ id: 'old-proj', message_count: 9, title: 'Old', ...stamp } as Partial<SessionInfo> & {
          title: string
        })
      )

      expect(stripe(container)).toBeNull()
    })
  }

  it('expires tree-only evidence without a row refresh', async () => {
    const { $kanbanObservationTick } = await import('@/store/kanban-observation')
    const { cleanStores } = await import('nanostores')
    cleanStores($kanbanObservationTick)
    vi.useFakeTimers()

    try {
      const { container } = renderRow(makeSession({ id: 'old-proj', message_count: 9, title: 'Old' }))
      expect(stripe(container)).toBe('done')
      act(() => vi.advanceTimersByTime(195_000))
      expect(stripe(container)).toBeNull()
      // Expired evidence is unknown, not running: the dot keeps its claim but says so.
      expect(container.querySelector('[aria-label="Open work, current status unknown"]')).not.toBeNull()
      expect(container.querySelector('[aria-label="Running in background"]')).toBeNull()
    } finally {
      cleanup()
      cleanStores($kanbanObservationTick)
      vi.useRealTimers()
    }
  })

  // [daveey] The background dot's words follow its evidence: unknown-only work never reads as "running".
  it('labels a row whose only open work is unknown as unknown, not running', () => {
    const { container } = renderRow(
      makeSession({
        execution_observations: [{ source: 'unavailable.work' }],
        id: 'unknown-only',
        kanban_active: 1,
        kanban_observed_at: Date.now() / 1000,
        kanban_parked: 1,
        message_count: 4,
        title: 'Parked remote'
      } as Partial<SessionInfo> & { title: string })
    )

    const dot = container.querySelector('[role="status"][aria-label="Open work, current status unknown"]')
    expect(dot).not.toBeNull()
    expect(dot?.getAttribute('title')).toBe('Open work, current status unknown')
    expect(container.querySelector('[aria-label="Running in background"]')).toBeNull()
  })

  it('keeps the running label when this window polls a live process for the unknown-only row', async () => {
    const polled = (await import('@/store/composer-status')).$backgroundRunningSessionIds as unknown as {
      set: (ids: string[]) => void
    }

    polled.set(['unknown-polled'])

    try {
      const { container } = renderRow(
        makeSession({
          id: 'unknown-polled',
          kanban_unknown: true,
          message_count: 4,
          title: 'Polled'
        } as Partial<SessionInfo> & {
          title: string
        })
      )

      expect(container.querySelector('[aria-label="Running in background"]')).not.toBeNull()
      expect(container.querySelector('[aria-label="Open work, current status unknown"]')).toBeNull()
    } finally {
      polled.set([])
    }
  })

  it('keeps the running label for a row with a running card', async () => {
    const fixture = await import('../../../../../../schemas/fixtures/execution-observations-v1.json')
    vi.useFakeTimers()
    vi.setSystemTime(1001_000)

    try {
      const { container } = renderRow(
        makeSession({
          execution_observations: [fixture.base],
          id: 'running-card',
          kanban_active: 1,
          kanban_observed_at: 1001,
          kanban_parked: 0,
          message_count: 4,
          title: 'Running card'
        } as Partial<SessionInfo> & { title: string })
      )

      expect(
        container.querySelector('[role="status"][aria-label="Running in background"]')?.getAttribute('title')
      ).toBe('Running in background')
      expect(container.querySelector('[aria-label="Open work, current status unknown"]')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the running label when a live process holds a row with unfresh card evidence', () => {
    const { container } = renderRow(
      makeSession({
        background_active: 1,
        id: 'proc-row',
        kanban_observed_at: undefined,
        message_count: 4,
        title: 'Watcher'
      } as Partial<SessionInfo> & {
        title: string
      })
    )

    expect(container.querySelector('[aria-label="Running in background"]')).not.toBeNull()
    expect(container.querySelector('[aria-label="Open work, current status unknown"]')).toBeNull()
  })

  // [daveey] Open kanban cards: never green and never blank — the grey
  // `waiting` card glyph (parked: ⌘] / ⌘[ skip it).
  it('shows the grey waiting glyph while kanban_active is set', () => {
    const { container } = renderRow(
      makeSession({ id: 'old-proj', kanban_active: 2, message_count: 9, title: 'Old' } as Partial<SessionInfo> & {
        title: string
      })
    )

    expect(stripe(container)).toBe('waiting')
    const glyph = container.querySelector('[data-slot="session-status-stripe"]')
    expect(glyph?.getAttribute('data-status-stripe')).toBe('waiting')
    expect(glyph?.querySelector('.codicon-project')).not.toBeNull()
  })

  it('refreshes goal completion without painting a finished dot while a blocked card remains', () => {
    const row = makeSession({
      id: 'goal-kanban-project',
      title: 'Goal complete',
      message_count: 9,
      goal_active: true,
      kanban_active: 1,
      kanban_blocked: 1,
      kanban_unseen: 1
    })

    const props = {
      isPinned: false,
      isSelected: false,
      onArchive: noop,
      onDelete: noop,
      onPin: noop,
      onResume: noop,
      onToggleUnread: noop,
      unread: false
    }

    certifyRow(row)
    const { container, rerender } = render(<SidebarSessionRow {...props} session={row} />)
    expect(stripe(container)).toBe('stuck')

    rerender(<SidebarSessionRow {...props} session={{ ...row, goal_active: false, kanban_active: 0 }} />)
    expect(stripe(container)).toBe('stuck')
    expect(screen.queryByLabelText('Finished')).toBeNull()

    rerender(<SidebarSessionRow {...props} session={{ ...row, goal_active: false, kanban_blocked: 0 }} />)
    expect(stripe(container)).toBe('waiting')
    expect(screen.queryByLabelText('Finished')).toBeNull()

    rerender(
      <SidebarSessionRow {...props} session={{ ...row, goal_active: false, kanban_active: 0, kanban_blocked: 0 }} />
    )
    expect(stripe(container)).toBe('done')
    expect(screen.getByLabelText('Finished')).toBeTruthy()
  })

  it('a blocked card beats waiting (red)', () => {
    const { container } = renderRow(
      makeSession({ kanban_active: 1, kanban_blocked: 1, message_count: 9, title: 'Old' } as Partial<SessionInfo> & {
        title: string
      })
    )

    expect(stripe(container)).toBe('stuck')
  })

  it('still paints done (green) with no activity stamps', () => {
    const { container } = renderRow(makeSession({ id: 'old-proj', message_count: 9, title: 'Old' }))

    expect(stripe(container)).toBe('done')
  })
})

// [daveey] A finished chat held off green only by a background process says
// so: a process glyph beside the kanban badge, hover lists it with Stop.
describe('SidebarSessionRow background process badge', () => {
  const proc = { command: 'sleep 600', id: 'proc_a' }

  const held = (extra: Partial<SessionInfo> = {}) =>
    makeSession({
      background_active: 1,
      background_processes: [{ ...proc, started_at: Date.now() / 1000 - 125 }],
      id: 'held',
      message_count: 4,
      title: 'Held',
      ...extra
    } as Partial<SessionInfo> & { title: string })

  it('hides the process badge on flat rows but keeps the row off green', () => {
    const { container } = renderRow(held())
    expect(screen.queryByTestId('session-row-background-processes')).toBeNull()
    expect(container.querySelector('[data-sidebar-stripe]')).toBeNull()
  })

  it('hides the count on the one-line row but preserves it on the card', () => {
    const two = held({
      background_active: 2,
      background_processes: [proc, { command: 'gh run watch 1', id: 'proc_b' }]
    } as Partial<SessionInfo>)

    renderRow(two)
    expect(screen.queryByTestId('session-row-background-processes')).toBeNull()
    cleanup()
    renderRow(two, { card: true })
    expect(screen.getByTestId('session-row-background-processes').textContent).toBe('2')
  })

  it('hover lists the command, its age and a Stop button', async () => {
    renderRow(held(), { card: true })
    fireEvent.pointerEnter(screen.getByTestId('session-row-background-processes'))

    const card = await screen.findByTestId('session-row-background-processes-card')
    expect(card.textContent).toContain('sleep 600')
    expect(card.textContent).toContain('5m') // coarseElapsed is mocked to 5 minutes above
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
  })

  it('click opens the session', () => {
    const onResume = vi.fn()
    renderRow(held(), { card: true, onResume })
    fireEvent.click(screen.getByTestId('session-row-background-processes'))

    expect(onResume).toHaveBeenCalledTimes(1)
  })

  it('is gone once the process exits', () => {
    const { container } = renderRow(makeSession({ id: 'held', message_count: 4, title: 'Held' }))

    expect(screen.queryByTestId('session-row-background-processes')).toBeNull()
    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('done')
  })
})

it('passes the exact selected owner to both row action menus', () => {
  menuProps.mockClear()

  const session = makeSession({
    title: 'Cached owner',
    connection_id: 'conn-mini',
    profile: 'work',
    offline_cached: true
  })

  renderRow(session)
  expect(menuProps.mock.calls).toHaveLength(2)

  for (const [props] of menuProps.mock.calls) {
    expect(props.selectedRow).toBe(session)
  }
})

it('does not paint a retained live turn as running on an offline row', () => {
  act(() => publishSessionState('offline-rt', { ...createClientSessionState('s1'), busy: true }))

  const { container } = renderRow(
    makeSession({
      title: 'Offline retained turn',
      offline_cached: true,
      is_active: false,
      goal_active: true,
      background_active: 1
    })
  )

  expect(container.querySelector('[data-working="true"]')).toBeNull()
  expect(screen.queryByLabelText('Running')).toBeNull()
  expect(screen.queryByTestId('session-row-background-processes')).toBeNull()
  expect(container.querySelector('[data-sidebar-stripe]')).toBeNull()
  act(() => clearAllSessionStates())
})

// [daveey] session-outcome: the stored outcome paints the stripe and explains it on hover.
describe('SidebarSessionRow stored outcome', () => {
  afterEach(() => {
    clearAllSessionStates()
  })

  const stripeOf = (container: HTMLElement) => container.querySelector('[data-slot="session-status-stripe"]')

  it.each([
    [{ kind: 'done', summary: 'Shipped it' }, 'done', 'emerald', 'Shipped it'],
    [{ kind: 'decision', items: [{ id: 'us_1', question: 'Merge PR #12?' }] }, 'stuck', 'red', 'Merge PR #12?'],
    [{ kind: 'muted' }, 'blocked', 'zinc', 'Muted'],
    [
      { kind: 'waiting', on: [{ kind: 'capacity', id: 'hub', label: 'hub slot' }] },
      'waiting',
      'zinc',
      'capacity: hub slot'
    ],
    [{ kind: 'running', on: [] }, 'waiting', 'zinc', 'Running'],
    [{ kind: 'unresolved', reason: 'notice_tail' }, 'attention', 'amber', 'Unresolved: notice_tail']
  ] as const)('%j paints %s (%s) with its tooltip', (outcome, stripe, colour, tip) => {
    const { container } = renderRow(
      // Inputs the old logic would paint green, so the outcome is what decides.
      makeSession({ message_count: 4, outcome, title: 'Outcome' } as Partial<SessionInfo> & { title: string })
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe(stripe)
    expect(stripeOf(container)?.getAttribute('data-outcome')).toBe(outcome.kind)
    expect(stripeOf(container)?.className).toContain(colour)
    expect(stripeOf(container)?.getAttribute('aria-label')).toBe(tip)
  })

  it('without an outcome the row paints as before, with no outcome tooltip', () => {
    const { container } = renderRow(makeSession({ message_count: 4, title: 'Legacy' }))

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('done')
    expect(stripeOf(container)?.getAttribute('aria-label')).toBeNull()
    expect(stripeOf(container)?.getAttribute('aria-hidden')).toBe('true')
  })

  it('a live turn still overrides the outcome', () => {
    publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

    const { container } = renderRow(
      makeSession({
        message_count: 4,
        outcome: { kind: 'unresolved', reason: 'no_summary' },
        title: 'Busy'
      } as Partial<SessionInfo> & { title: string })
    )

    expect(container.querySelector('[data-sidebar-stripe]')).toBeNull()
  })

  // [daveey] A Kanban close-out turn the backend woke in a chat this window never opened: no stream state, only
  // the row's `turn_live` stamp. The decision must yield to it (working, no red stripe), and paint red again after.
  it('a backend-reported running turn (turn_live) overrides a decision outcome; without it the decision is red', () => {
    const decision = { kind: 'decision', items: [{ id: 'us_1', question: 'Merge PR #12?' }] }

    const live = renderRow(
      makeSession({
        message_count: 4,
        outcome: decision,
        title: 'Close-out',
        turn_live: true
      } as Partial<SessionInfo> & { title: string })
    )

    expect(live.container.querySelector('[data-sidebar-stripe]')).toBeNull()
    expect(live.container.querySelector('[data-working="true"]')).not.toBeNull()
    live.unmount()

    const settled = renderRow(
      makeSession({ message_count: 4, outcome: decision, title: 'Close-out' } as Partial<SessionInfo> & {
        title: string
      })
    )

    expect(settled.container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('stuck')
    expect(settled.container.querySelector('[data-working="true"]')).toBeNull()
  })

  it('renders initial partial roster uncertainty beside live work and repaints clock-only expiry', async () => {
    const { $liveWorkRoster, setLiveWorkRosterSource } = await import('@/store/live-work-roster')
    const { gatewayRouteState } = await import('@/store/gateway')
    const { $kanbanObservationTick } = await import('@/store/kanban-observation')
    const fixture = await import('../../../../../../schemas/fixtures/execution-observations-v1.json')
    vi.useFakeTimers()
    vi.setSystemTime(1001000)
    const session = makeSession({ id: 'roster-row', title: 'Roster', message_count: 2 })
    const source = gatewayRouteState(null, 'default').key

    const record = {
      ...fixture.base,
      identity: { ...fixture.base.identity, kind: 'process', session: 'roster-row' },
      valid_until: 1010
    }

    setLiveWorkRosterSource(
      source,
      [{ session_id: session.id, background: 1, subagents: 1, execution_observations: [record] }],
      1001000,
      [session.id]
    )

    try {
      const { container } = renderRow(session)
      expect(container.querySelector('[data-testid="session-row-card-squares"]')?.getAttribute('aria-label')).toBe(
        'Waiting 1'
      )
      await act(async () => {
        vi.setSystemTime(1011000)
        $kanbanObservationTick.set($kanbanObservationTick.get() + 1)
      })
      expect(container.querySelector('[data-testid="session-row-card-squares"]')?.getAttribute('aria-label')).toBe(
        'Waiting 2'
      )
    } finally {
      $liveWorkRoster.set({})
      vi.useRealTimers()
    }
  })

  it('reads the park from row.muted', () => {
    const { container } = renderRow(
      makeSession({ message_count: 4, muted: true, title: 'Parked' } as Partial<SessionInfo> & { title: string })
    )

    expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('blocked')
  })

  // [daveey] desktop.log `state=unread … outcome=waiting`, every card parked: the dot painted the emerald unread
  // colour and read as done. An unread dot takes the outcome's tone unless the outcome is done.
  describe('unread dot against the stored outcome', () => {
    afterEach(() => {
      $unreadFinishedSessionIds.set([])
    })

    const dotOf = (container: HTMLElement) =>
      container.querySelector('[role="status"][aria-label^="Unread"], [role="status"][aria-label="Finished"]')

    it.each([
      [{ kind: 'waiting', on: [{ kind: 'card', id: 't_1', state: 'scheduled' }] }, 'zinc', 'Unread — Waiting'],
      [{ kind: 'running', on: [] }, 'zinc', 'Unread — Running'],
      [{ kind: 'unresolved', reason: 'notice_tail' }, 'amber', 'Unread — Unresolved: notice_tail'],
      [{ kind: 'decision', items: [{ id: 'us_1', question: 'Merge?' }] }, 'red', 'Unread — Waiting on your decision']
    ] as const)('an unread %j chat paints %s, never the done emerald', (outcome, colour, label) => {
      $unreadFinishedSessionIds.set(['s1'])

      const { container } = renderRow(
        makeSession({
          kanban_active: 2,
          kanban_parked: 2,
          message_count: 4,
          outcome,
          title: 'Parked cards'
        } as Partial<SessionInfo> & { title: string })
      )

      const dot = dotOf(container)
      expect(dot?.getAttribute('aria-label')).toBe(label)
      expect(dot?.className).toContain(colour)
      expect(dot?.className).not.toContain('ui-success')
    })

    // [daveey] Muted maps to the `blocked` stripe; session-outcome.test.ts pins that mapping, this pins the row
    // actually paints it: a parked chat that comes back unread must not show the done emerald.
    it('an unread muted chat paints the blocked stripe tone, never the done emerald', () => {
      $unreadFinishedSessionIds.set(['s1'])

      const { container } = renderRow(
        makeSession({
          message_count: 4,
          muted: true,
          outcome: { kind: 'muted' },
          title: 'Parked'
        } as Partial<SessionInfo> & { title: string })
      )

      const dot = dotOf(container)
      expect(dot?.getAttribute('aria-label')).toBe('Unread — Muted')
      expect(dot?.className).toContain(sessionStatusStripeColor('blocked'))
      expect(dot?.className).not.toContain(sessionStatusStripeColor('done'))
      expect(dot?.className).not.toContain('ui-success')
      expect(container.querySelector('[data-sidebar-stripe]')?.getAttribute('data-sidebar-stripe')).toBe('blocked')
    })

    it('an unread done chat (or one with no outcome) keeps the emerald unread dot', () => {
      $unreadFinishedSessionIds.set(['s1'])

      for (const outcome of [{ kind: 'done', summary: 'Shipped' }, undefined]) {
        const { container, unmount } = renderRow(
          makeSession({ message_count: 4, outcome, title: 'Done' } as Partial<SessionInfo> & { title: string })
        )

        const dot = dotOf(container)
        expect(dot?.getAttribute('aria-label')).toBe('Finished')
        expect(dot?.className).toContain('ui-success')
        unmount()
      }
    })
  })
})
