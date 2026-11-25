import { useStore } from '@nanostores/react'
// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { type ReactNode, useEffect } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $apiRequestScope } from '@/api/client'
import { useKeybinds } from '@/app/hooks/use-keybinds'
import { group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { SidebarProvider } from '@/components/ui/sidebar'
import { registry } from '@/contrib/registry'
import { isMacPlatform } from '@/lib/platform'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import {
  $pinnedSessionIds,
  $sidebarMessagingOpenIds,
  $sidebarOpen,
  $sidebarProjectFilter,
  $sidebarShowArchived,
  $sidebarShowAutomated,
  $sidebarStatusFilter,
  requestSessionSearchFocus,
  resetSidebarView,
  setSidebarAgentsGrouped,
  setSidebarGrouping,
  setSidebarOpen
} from '@/store/layout'
import { $activeGatewayProfile, $profiles, setShowAllProfiles } from '@/store/profile'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import { $projectTree } from '@/store/projects'
import {
  $currentCwd,
  $messagingSessions,
  $messagingTruncated,
  $selectedStoredSessionId,
  $sessions,
  $sessionsLoadError,
  $sessionsLoading,
  $workspaceCwdOwner
} from '@/store/session'
import { $sidebarSessionsPrePlugin, SESSION_FILTER_AREA } from '@/store/session-filter'
import { $removedSessionIds } from '@/store/session-removal'
import { $archivedSessions } from '@/store/sidebar-archive'
import { SIDEBAR_NAV_PREFS_AREA } from '@/store/sidebar-nav'
import { makeSessionInfo } from '@/test/session-info'

import {
  type AppView,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  SIDEBAR_SECTIONS_AREA,
  SIDEBAR_SECTIONS_TOP_AREA
} from '../../routes'

import { $gatewayGroupCollapsed } from './gateway-group-preferences'

import { ChatSidebar } from './index'

// The Archived view reads its own pages through this one call; everything else in the module stays real.
const listAllProfileSessions = vi.hoisted(() => vi.fn())

vi.mock('@/api/sessions', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listAllProfileSessions
}))

beforeEach(() => {
  listAllProfileSessions.mockReset().mockResolvedValue({ sessions: [], total: 0 })
})

it('mounts the top SDK section before real Pinned and leaves legacy sections below it', () => {
  const top = registry.register({
    area: SIDEBAR_SECTIONS_TOP_AREA,
    id: 'lanes-test',
    render: () => <div>LANES fixture</div>
  })

  const legacy = registry.register({
    area: SIDEBAR_SECTIONS_AREA,
    id: 'legacy-test',
    render: () => <div>Legacy fixture</div>
  })

  try {
    $sessions.set(sessionRows)
    $pinnedSessionIds.set(['tile-one'])
    renderSidebar('/chat', 'chat')
    const lanes = screen.getByText('LANES fixture')
    const pinned = screen.getByText('Pinned')
    const old = screen.getByText('Legacy fixture')
    expect(lanes.compareDocumentPosition(pinned) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(pinned.compareDocumentPosition(old) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  } finally {
    top()
    legacy()
  }
})

const noop = () => {}

vi.mock('@/themes/context', () => ({
  useTheme: () => ({ resolvedMode: 'light', setMode: vi.fn() })
}))

const noopAsync = async () => {}

const sessionRows = [
  makeSessionInfo({ id: 'tile-one', last_active: 2, profile: 'default', started_at: 1, title: 'Tile one' }),
  makeSessionInfo({ id: 'tile-two', last_active: 2, profile: 'default', started_at: 1, title: 'Tile two' })
]

const renderSidebar = (pathname: string, currentView: AppView, onRetrySessions: () => Promise<void> = noopAsync) =>
  render(
    <MemoryRouter initialEntries={[pathname]}>
      <SidebarProvider>
        <ChatSidebar
          currentView={currentView}
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={onRetrySessions}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

const currentButtons = () =>
  screen.queryAllByRole('button').filter(button => button.classList.contains('bg-(--ui-control-active-background)'))

const expectOnlyCurrent = (label: string | null) => {
  const button = label ? screen.getByRole('button', { name: label }) : null

  expect(currentButtons()).toEqual(button ? [button] : [])
}

const expectOnlySelectedSession = (title: string | null) => {
  const rows = ['Tile one', 'Tile two']
    .map(label => screen.queryByText(label)?.closest('.group.row-hover'))
    .filter(row => row !== undefined)

  const selectedRows = rows.filter(row => row?.className.includes('bg-(--ui-row-active-background)'))
  const expected = title ? [screen.getByText(title).closest('.group.row-hover')] : []

  expect(selectedRows).toEqual(expected)
}

const focus = (groupId: null | string) => act(() => noteActiveTreeGroup(groupId))

describe('ChatSidebar search disclosure', () => {
  const deps = {
    archiveSelectedSession: vi.fn(),
    openNewSessionTab: vi.fn(),
    requestGateway: vi.fn(),
    startFreshSession: vi.fn(),
    toggleCommandCenter: vi.fn(),
    toggleSelectedPin: vi.fn()
  }

  const search = () => screen.queryByRole('textbox', { name: 'Search sessions' })

  const press = (key: string, shiftKey = false) =>
    act(() => {
      fireEvent.keyDown(document.activeElement ?? document.body, {
        key,
        code: `Key${key.toUpperCase()}`,
        shiftKey,
        metaKey: isMacPlatform(),
        ctrlKey: !isMacPlatform()
      })
    })

  const reveal = async () => {
    press('f', true)
    await waitFor(() => expect(search()).toBe(document.activeElement))
    expect(search()).not.toBeNull()

    return search() as HTMLInputElement
  }

  beforeEach(() => {
    resetSidebarView()
    $sessions.set(sessionRows)
    $removedSessionIds.set(new Set())
    $sessionsLoading.set(false)
    renderHook(() => useKeybinds(deps), {
      wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>
    })
  })
  afterEach(() => {
    cleanup()
    $sessions.set([])
    vi.clearAllMocks()
  })

  it('omits the New session row, its chevron and the initial search field', () => {
    const { container } = renderSidebar('/', 'chat')
    expect(container.querySelector('[data-tour="sidebar-nav-new-session"]')).toBeNull()
    expect(screen.queryByRole('button', { name: /Collapse nav|Expand nav/ })).toBeNull()
    expect(search()).toBeNull()
  })

  it('reveals and focuses via the existing shortcut and refocuses one existing input', async () => {
    renderSidebar('/', 'chat')
    const input = await reveal()
    fireEvent.change(input, { target: { value: 'Tile' } })
    act(() => input.blur())
    await reveal()
    expect(search()).toBe(input)
    expect(input.value).toBe('Tile')
    expect(screen.getAllByRole('textbox', { name: 'Search sessions' })).toHaveLength(1)
  })

  it('opens a closed sidebar before revealing and focusing the newly mounted field', async () => {
    setSidebarOpen(false)

    function ClosedSidebar() {
      const open = useStore($sidebarOpen)

      return open ? (
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={noopAsync}
          onTriggerCronJob={noopAsync}
        />
      ) : null
    }

    render(
      <MemoryRouter>
        <SidebarProvider>
          <ClosedSidebar />
        </SidebarProvider>
      </MemoryRouter>
    )
    expect(search()).toBeNull()
    await reveal()
    expect($sidebarOpen.get()).toBe(true)
  })

  it('Escape clears the active query and hides; reopening starts empty', async () => {
    renderSidebar('/', 'chat')
    const input = await reveal()
    fireEvent.change(input, { target: { value: 'Tile one' } })
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    act(() => input.dispatchEvent(escape))
    expect(escape.defaultPrevented).toBe(true)
    expect(search()).toBeNull()
    expect((await reveal()).value).toBe('')
  })

  it('dismisses empty and whitespace-only search on blur', async () => {
    renderSidebar('/', 'chat')
    act(() => requestSessionSearchFocus())
    await waitFor(() => expect(search()).toBe(document.activeElement))
    expect(search()).not.toBeNull()
    act(() => (search() as HTMLInputElement).blur())
    expect(search()).toBeNull()
    const input = await reveal()
    fireEvent.change(input, { target: { value: '   ' } })
    act(() => input.blur())
    expect(search()).toBeNull()
  })

  it('retains a nonempty filter on blur and keeps the existing results section', async () => {
    renderSidebar('/', 'chat')
    const input = await reveal()
    fireEvent.change(input, { target: { value: 'Tile one' } })
    act(() => input.blur())
    expect(search()).toBe(input)
    expect(input.value).toBe('Tile one')
    expect(screen.getByText('Results')).toBeTruthy()
    expect(screen.getByText('Tile one')).toBeTruthy()
    expect(screen.queryByText('Tile two')).toBeNull()
  })

  it('still invokes session.new with the default mod+n chord', () => {
    renderSidebar('/', 'chat')
    press('n')
    expect(deps.startFreshSession).toHaveBeenCalledTimes(1)
    expect(search()).toBeNull()
  })

  it('can disclose search even in the empty-session blank state', async () => {
    $sessions.set([])
    $messagingSessions.set([])
    renderSidebar('/', 'chat')
    expect(screen.getByText('No sessions yet')).toBeTruthy()
    const input = await reveal()
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    expect(search()).toBe(input)
    fireEvent.change(input, { target: { value: 'missing title' } })
    expect(screen.getByText('Results')).toBeTruthy()
    expect(screen.queryByText('No sessions yet')).toBeNull()
  })

  it('keeps remaining routes accessible even with a legacy collapsed preference', () => {
    localStorage.setItem('sidebar.nav.collapsed', '1')

    try {
      const { container } = renderSidebar('/', 'chat')
      expect(screen.getByRole('button', { name: 'Capabilities' })).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Messaging' })).toBeTruthy()
      expect(container.querySelector('[data-tour="sidebar-nav-new-session"]')).toBeNull()
    } finally {
      localStorage.removeItem('sidebar.nav.collapsed')
    }
  })

  it('clears and dismisses when the clear button is clicked after input blur', async () => {
    renderSidebar('/', 'chat')
    const input = await reveal()
    fireEvent.change(input, { target: { value: 'Tile one' } })
    const clear = screen.getByRole('button', { name: 'Clear search' })
    act(() => clear.focus())
    expect(search()).toBe(input)
    fireEvent.click(clear)
    expect(search()).toBeNull()
    expect(screen.queryByText('Results')).toBeNull()
    expect((await reveal()).value).toBe('')
  })
})

describe('ChatSidebar navigation activity', () => {
  let disposeContributions: () => void

  beforeEach(() => {
    disposeContributions = registry.registerMany([
      { area: ROUTES_AREA, id: 'kanban-page', data: { path: '/kanban' }, render: () => null },
      { area: ROUTES_AREA, id: 'reports-page', data: { path: '/reports' }, render: () => null },
      { area: SIDEBAR_NAV_AREA, id: 'kanban-nav', data: { codicon: 'project', label: 'Kanban', path: '/kanban' } },
      { area: SIDEBAR_NAV_AREA, id: 'reports-nav', data: { codicon: 'graph', label: 'Reports', path: '/reports' } }
    ])
    $selectedStoredSessionId.set('tile-one')
    $sessions.set(sessionRows)
    $removedSessionIds.set(new Set())
    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'workspace-group' }),
        group(['session-tile:tile-one'], { active: 'session-tile:tile-one', id: 'tile-one-group' }),
        group(['session-tile:tile-two'], { active: 'session-tile:tile-two', id: 'tile-two-group' })
      ])
    )
    noteActiveTreeGroup('workspace-group')
  })

  afterEach(() => {
    cleanup()
    disposeContributions()
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    $removedSessionIds.set(new Set())
    $layoutTree.set(null)
    noteActiveTreeGroup(null)
  })

  it('keeps list-dependent plugin registration stable while reflecting core input changes', () => {
    resetSidebarView()
    renderSidebar('/', 'chat')
    let registrations = 0

    function Consumer() {
      // This is the SDK useSidebarSessions hook's store subscription.
      const sessions = useStore($sidebarSessionsPrePlugin)

      useEffect(() => {
        registrations++

        // Bound the reproduction instead of crashing React with an infinite loop.
        if (registrations >= 8) {
          return
        }

        return registry.register({
          area: SESSION_FILTER_AREA,
          id: 'list-consumer',
          data: { match: (s: { id: string }) => sessions.some(row => row.id === s.id) }
        })
      }, [sessions])

      return null
    }

    const consumer = render(<Consumer />)

    try {
      expect(registrations).toBe(1)
      expect($sidebarSessionsPrePlugin.get().map(row => row.id)).toEqual(['tile-one', 'tile-two'])

      act(() => $sessions.set([sessionRows[0]]))
      expect(registrations).toBe(2)
      expect($sidebarSessionsPrePlugin.get().map(row => row.id)).toEqual(['tile-one'])

      act(() => $sidebarStatusFilter.set(['draft']))
      expect(registrations).toBe(3)
      expect($sidebarSessionsPrePlugin.get()).toEqual([])

      act(() => $sidebarStatusFilter.set([]))
      expect(registrations).toBe(4)
      expect($sidebarSessionsPrePlugin.get().map(row => row.id)).toEqual(['tile-one'])
    } finally {
      consumer.unmount()
      act(() => resetSidebarView())
    }
  })

  it('keeps navigation and session activity coherent with the focused pane', () => {
    renderSidebar('/kanban', 'extension')
    expectOnlyCurrent('Kanban')
    expectOnlySelectedSession(null)

    focus('tile-one-group')
    expectOnlyCurrent(null)
    expectOnlySelectedSession('Tile one')

    focus('tile-two-group')
    expectOnlyCurrent(null)
    expectOnlySelectedSession('Tile two')

    focus(null)
    expectOnlyCurrent('Kanban')
    expectOnlySelectedSession(null)

    focus('tile-two-group')
    act(() => {
      $removedSessionIds.set(new Set(['tile-two']))
      $sessions.set([sessionRows[0]])
    })
    expectOnlyCurrent(null)
    expectOnlySelectedSession(null)

    act(() => {
      $removedSessionIds.set(new Set())
      $sessions.set(sessionRows)
    })

    for (const [pathname, currentView, label] of [
      ['/capabilities', 'capabilities', 'Capabilities'],
      ['/messaging', 'messaging', 'Messaging'],
      ['/artifacts', 'artifacts', 'Artifacts'],
      ['/cron', 'cron', 'Scheduled jobs']
    ] as const) {
      cleanup()
      focus('workspace-group')
      renderSidebar(pathname, currentView)
      expectOnlyCurrent(label)
      expectOnlySelectedSession(null)

      focus('tile-one-group')
      expectOnlyCurrent(null)
      expectOnlySelectedSession('Tile one')
    }

    cleanup()
    focus('workspace-group')
    renderSidebar('/reports', 'extension')
    expectOnlyCurrent('Reports')

    cleanup()
    disposeContributions()
    disposeContributions = noop
    focus('workspace-group')
    renderSidebar('/kanban', 'extension')
    expect(screen.queryByRole('button', { name: 'Kanban' })).toBeNull()
    expectOnlyCurrent(null)
    expectOnlySelectedSession(null)
  })

  // Teardown proof: the loader disposes a plugin's contributions on disable,
  // and that disposer alone must bring the row back — no store to clear.
  it('hides a nav row while a sidebarNav.prefs contribution is registered and restores it on dispose', () => {
    renderSidebar('/kanban', 'extension')
    expect(screen.getByRole('button', { name: 'Kanban' })).toBeTruthy()

    let dispose = () => {}

    act(() => {
      dispose = registry.register({ area: SIDEBAR_NAV_PREFS_AREA, id: 'prefs', data: { hide: ['kanban-nav'] } })
    })
    expect(screen.queryByRole('button', { name: 'Kanban' })).toBeNull()
    // A hidden row is a preference, not a removal: the sibling nav row stays.
    expect(screen.getByRole('button', { name: 'Reports' })).toBeTruthy()

    act(() => dispose())
    expect(screen.getByRole('button', { name: 'Kanban' })).toBeTruthy()
  })

  it('keeps plugin-only empty filters visible and makes Reset call onClear', async () => {
    resetSidebarView()
    $sessionsLoading.set(false)
    renderSidebar('/', 'chat')
    let dispose = noop
    const onClear = vi.fn(() => dispose())

    act(() => {
      dispose = registry.register({
        area: SESSION_FILTER_AREA,
        id: 'empty-lane',
        data: { match: () => false, onClear }
      })
    })

    try {
      expect(screen.queryByText('No sessions yet')).toBeNull()
      expect(screen.getAllByText('No sessions match these filters').length).toBeGreaterThan(0)
      fireEvent.keyDown(screen.getByRole('button', { name: 'Filters' }), { key: 'ArrowDown' })
      fireEvent.click(await screen.findByText('Reset to defaults'))
      expect(onClear).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Tile one')).toBeTruthy()
    } finally {
      act(() => dispose())
      $sessionsLoading.set(true)
    }
  })

  // `sessions.filter`: a contribution hides non-matching rows, its disposer
  // restores them, and clearing the sidebar filters calls its onClear.
  it('hides non-matching session rows while a sessions.filter contribution is registered', () => {
    renderSidebar('/', 'chat')
    expect(screen.getByText('Tile one')).toBeTruthy()
    expect(screen.getByText('Tile two')).toBeTruthy()

    const onClear = vi.fn()

    let dispose = () => {}

    act(() => {
      dispose = registry.register({
        area: SESSION_FILTER_AREA,
        data: { match: (s: { id: string }) => s.id === 'tile-one', onClear },
        id: 'lane'
      })
    })
    expect(screen.getByText('Tile one')).toBeTruthy()
    expect(screen.queryByText('Tile two')).toBeNull()
    // The SDK list stays pre-plugin: both rows.
    expect($sidebarSessionsPrePlugin.get().map(s => s.id)).toEqual(['tile-one', 'tile-two'])

    act(() => resetSidebarView())
    expect(onClear).toHaveBeenCalledTimes(1)

    act(() => dispose())
    expect(screen.getByText('Tile two')).toBeTruthy()
  })
})

// #67600: a cold-start read that failed used to render as an empty account.
describe('ChatSidebar cold-start load failure', () => {
  afterEach(() => {
    cleanup()
    $sessions.set([])
    $sessionsLoading.set(true)
    $sessionsLoadError.set(false)
  })

  it('offers retry in place of the empty state', () => {
    const retry = vi.fn(noopAsync)
    $sessions.set([])
    $sessionsLoading.set(false)
    $sessionsLoadError.set(true)

    renderSidebar('/', 'chat', retry)

    expect(screen.getByText('Could not load sessions')).toBeTruthy()
    expect(screen.queryByText('No sessions yet')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(retry).toHaveBeenCalledTimes(1)
  })
})

// #146510: see SidebarPinnedEmptyState.
describe('ChatSidebar pinned empty state under filters', () => {
  let previousShowAutomated: boolean

  beforeEach(() => {
    previousShowAutomated = $sidebarShowAutomated.get()
    $sidebarShowAutomated.set(true)
    $sidebarStatusFilter.set([])
    $sidebarProjectFilter.set([])
    $sidebarShowArchived.set(false)
    $pinnedSessionIds.set([])
    $sessions.set(sessionRows.map(row => ({ ...row, kanban_observed_at: Date.now() / 1000 })))
    $sessionsLoading.set(false)
  })

  afterEach(() => {
    cleanup()
    $sidebarShowAutomated.set(previousShowAutomated)
    $sidebarStatusFilter.set([])
    $sidebarProjectFilter.set([])
    $sidebarShowArchived.set(false)
    $pinnedSessionIds.set([])
    $sessions.set([])
    $sessionsLoading.set(true)
  })

  it('names the filter when it hides every pin', () => {
    $sessions.set([
      { ...sessionRows[0], kanban_observed_at: Date.now() / 1000, pinned: true },
      { ...sessionRows[1], kanban_observed_at: Date.now() / 1000 }
    ])
    $pinnedSessionIds.set(['tile-one'])
    $sidebarStatusFilter.set(['working'])

    renderSidebar('/', 'chat')

    expect(screen.queryByText('Tile one')).toBeNull()
    expect(screen.queryByText('Shift-click a chat to pin')).toBeNull()
    // Pinned and Sessions both report the filter.
    expect(screen.getAllByText('No sessions match these filters')).toHaveLength(2)
  })

  it('keeps the pin hint when nothing is pinned and no filter is on', () => {
    renderSidebar('/', 'chat')

    expect(screen.getByText('Shift-click a chat to pin')).toBeTruthy()
    expect(screen.queryByText('No sessions match these filters')).toBeNull()
  })

  it('keeps the pin hint when the only filter is a project id the tree does not resolve', () => {
    $sidebarProjectFilter.set(['/repos/gone'])

    renderSidebar('/', 'chat')

    expect(screen.getByText('Shift-click a chat to pin')).toBeTruthy()
  })

  it('drops the pin hint in the Archived view when Pinned is empty', async () => {
    $sidebarShowArchived.set(true)

    renderSidebar('/', 'chat')

    expect(screen.queryByText('Shift-click a chat to pin')).toBeNull()
    await waitFor(() => expect(screen.getAllByText('No sessions match these filters')).toHaveLength(2))
  })
})

// 2026-11-17: with 16,899 archived rows the view read every page before listing any, and said
// "No sessions match these filters" the whole time.
describe('ChatSidebar Archived view paging', () => {
  const archivedRow = (id: string, title: string) =>
    makeSessionInfo({ archived: true, id, last_active: 2, profile: 'default', started_at: 1, title })

  let previousShowAutomated: boolean

  beforeEach(() => {
    previousShowAutomated = $sidebarShowAutomated.get()
    $sidebarShowAutomated.set(true)
    $sidebarStatusFilter.set([])
    $sidebarProjectFilter.set([])
    $pinnedSessionIds.set([])
    $sessions.set([])
    $sessionsLoading.set(false)
    $archivedSessions.set([])
    $sidebarShowArchived.set(true)
  })

  afterEach(() => {
    cleanup()
    $sidebarShowAutomated.set(previousShowAutomated)
    $sidebarShowArchived.set(false)
    $archivedSessions.set([])
    $sessionsLoading.set(true)
    setSidebarAgentsGrouped(false)
  })

  it('lists the first page and reads the next one from the load-more row, whatever the grouping', async () => {
    setSidebarAgentsGrouped(true)
    listAllProfileSessions
      .mockReset()
      .mockResolvedValueOnce({ sessions: [archivedRow('old-one', 'Old one')], total: 16_899 })
      .mockResolvedValueOnce({ sessions: [archivedRow('old-two', 'Old two')], total: 16_899 })

    renderSidebar('/', 'chat')

    expect(await screen.findByText('Old one')).toBeTruthy()
    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Load more' })))

    expect(await screen.findByText('Old two')).toBeTruthy()
    expect(screen.getByText('Old one')).toBeTruthy()
    // One page per click, even though it revealed a single row.
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect(listAllProfileSessions).toHaveBeenLastCalledWith(500, 0, 'only', 'recent', 'all', {}, 500)
  })

  it('offers no load-more row once the archive is fully listed', async () => {
    listAllProfileSessions.mockReset().mockResolvedValue({ sessions: [archivedRow('old-one', 'Old one')], total: 1 })

    renderSidebar('/', 'chat')

    await screen.findByText('Old one')

    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('does not keep the load-more row disabled for a new scope while the old read is stranded', async () => {
    let stranded = false

    listAllProfileSessions.mockReset().mockImplementation(async (...args: unknown[]) => {
      if ((args[6] as number) > 0) {
        stranded = true

        // The old scope's second page never comes back within this test.
        return new Promise(() => {})
      }

      return { sessions: [archivedRow('old-one', 'Old one')], total: 16_899 }
    })

    renderSidebar('/', 'chat')
    await screen.findByText('Old one')
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Load more' })))
    await waitFor(() => expect(stranded).toBe(true))

    await act(async () => $apiRequestScope.setKey('connectionId', 'other-gateway'))

    await waitFor(() =>
      expect((screen.getByRole('button', { name: 'Load more' }) as HTMLButtonElement).disabled).toBe(false)
    )
    $apiRequestScope.setKey('connectionId', null)
  })

  it('does not call an archive that is still loading empty', async () => {
    let finish!: (page: unknown) => void
    listAllProfileSessions.mockReset().mockImplementation(() => new Promise(resolve => (finish = resolve)))

    renderSidebar('/', 'chat')

    // Pinned has nothing to load, so it alone states its empty filter result.
    expect(screen.getAllByText('No sessions match these filters')).toHaveLength(1)

    await act(async () => finish({ sessions: [], total: 0 }))

    await waitFor(() => expect(screen.getAllByText('No sessions match these filters')).toHaveLength(2))
  })
})

// Entering a project is a scope switch: the conversation main is showing keeps
// its workspace, so Files/Review and the composer's Git context can't drift to
// the project while the transcript stays on the old chat (#72772).
describe('ChatSidebar project entry', () => {
  const project = {
    id: '/repos/new-project',
    label: 'new-project',
    path: '/repos/new-project',
    repos: [],
    sessionCount: 0
  }

  beforeEach(() => {
    setSidebarAgentsGrouped(true)
    $projectTree.set([project])
    $currentCwd.set('/repos/old-project')
  })

  afterEach(() => {
    cleanup()
    $projectScope.set(ALL_PROJECTS)
    $projectTree.set([])
    setSidebarAgentsGrouped(false)
    $currentCwd.set('')
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    $sessions.set([])
  })

  it("leaves a stored conversation's workspace alone", () => {
    $sessions.set(sessionRows)
    $selectedStoredSessionId.set('tile-one')
    $workspaceCwdOwner.set('tile-one')
    $projectScope.set(project.id)

    renderSidebar('/tile-one', 'chat')

    expect($currentCwd.get()).toBe('/repos/old-project')
    expect($workspaceCwdOwner.get()).toBe('tile-one')
  })

  it('re-homes a fresh draft into the entered project', () => {
    $projectScope.set(project.id)

    renderSidebar('/', 'chat')

    expect($currentCwd.get()).toBe(project.path)
  })

  it('stays inside an entered empty auto project instead of bouncing to the overview', () => {
    // A non-empty sibling keeps the overview live, which is what arms the scope reset.
    const sibling = { ...project, id: '/repos/busy', label: 'busy', path: '/repos/busy', sessionCount: 2 }

    $projectTree.set([{ ...project, isAuto: true, sessionCount: 0 }, sibling])
    $projectScope.set(project.id)

    renderSidebar('/', 'chat')

    expect($projectScope.get()).toBe(project.id)
  })
})

// A brand-new chat in a folder no project claims (a CLI started in ~) has no
// backend owner yet, and the tree may carry no Home node at all (Home only
// exists once a persisted row lands there). It must still show in the grouped
// overview at once — under Home, not nowhere — and a fresh chat in a known
// repo's subfolder must show under that repo only, never twice.
describe('ChatSidebar fresh chat before the tree knows it', () => {
  const repoRow = makeSessionInfo({
    cwd: '/work/repo',
    git_repo_root: '/work/repo',
    id: 'repo-row',
    last_active: 5,
    message_count: 3,
    profile: 'default',
    started_at: 5,
    title: 'Repo chat'
  })

  const repoProject = {
    id: '/work/repo',
    isAuto: true,
    label: 'repo',
    path: '/work/repo',
    previewSessions: [repoRow],
    repos: [{ groups: [], id: '/work/repo', label: 'repo', path: '/work/repo', sessionCount: 1 }],
    sessionCount: 1,
    sessionIds: ['repo-row']
  }

  const fresh = (cwd: string, id: string, title: string) =>
    makeSessionInfo({ cwd, id, last_active: 10, message_count: 0, profile: 'default', started_at: 10, title })

  beforeEach(() => {
    setSidebarAgentsGrouped(true)
    $projectTree.set([repoProject])
  })

  afterEach(() => {
    cleanup()
    $projectScope.set(ALL_PROJECTS)
    $projectTree.set([])
    setSidebarAgentsGrouped(false)
    $sessions.set([])
  })

  // The overview project row a rendered chat sits under.
  const rowOwner = (title: string) =>
    screen.getByText(title).closest('[data-sessions-project]')?.getAttribute('data-sessions-project')

  it('shows a fresh zero-message home-folder chat under a Home row the tree never sent', () => {
    $sessions.set([repoRow, fresh('/Users/example', 'fresh-home', 'Fresh home chat')])

    renderSidebar('/', 'chat')

    expect(screen.getAllByText('Fresh home chat')).toHaveLength(1)
    expect(rowOwner('Fresh home chat')).toBe('__no_project__')
    expect(rowOwner('Repo chat')).toBe('/work/repo')
  })

  it('lists the fresh home-folder chat when Home is entered before the tree has a Home node', () => {
    $sessions.set([repoRow, fresh('/Users/example', 'fresh-home', 'Fresh home chat')])
    $projectScope.set('__no_project__')

    renderSidebar('/', 'chat')

    expect($projectScope.get()).toBe('__no_project__')
    expect(screen.getAllByText('Fresh home chat')).toHaveLength(1)
    expect(screen.queryByText('Repo chat')).toBeNull()
  })

  it('files a fresh chat in a known repo subfolder under that repo only, even with a Home node', () => {
    const homeNode = {
      id: '__no_project__',
      isNoProject: true,
      label: 'Home',
      path: null,
      previewSessions: [],
      repos: [],
      sessionCount: 0,
      sessionIds: []
    }

    $projectTree.set([homeNode, repoProject])
    $sessions.set([repoRow, fresh('/work/repo/apps', 'fresh-sub', 'Fresh subfolder chat')])

    renderSidebar('/', 'chat')

    expect(screen.getAllByText('Fresh subfolder chat')).toHaveLength(1)
    expect(rowOwner('Fresh subfolder chat')).toBe('/work/repo')
  })
})

// Messaging platforms group rows by owner the same way recents does once every
// profile is on screen, so a Telegram thread is attributable to its profile
// (#87715). The platform's row cap and load-more stay the section's.
describe('ChatSidebar section labels', () => {
  const realProject = {
    id: '/repos/new-project',
    label: 'new-project',
    path: '/repos/new-project',
    repos: [],
    sessionCount: 1
  }

  // The synthetic Home bucket the backend emits when no project claimed a
  // session (no cwd / deleted workspace): one lane, no repo structure.
  const homeOnly = [
    {
      ...realProject,
      id: '__no_project__',
      label: 'Home',
      path: null,
      isNoProject: true,
      sessionCount: 2
    }
  ]

  afterEach(() => {
    cleanup()
    $projectScope.set(ALL_PROJECTS)
    $projectTree.set([])
    setSidebarAgentsGrouped(false)
    $sessions.set([])
  })

  it('labels the section "Projects" when the grouped tree has a real project', () => {
    setSidebarAgentsGrouped(true)
    $projectTree.set([realProject])

    renderSidebar('/', 'chat')

    expect(screen.getByText('Projects')).toBeTruthy()
    expect(screen.queryByText('Sessions')).toBeNull()
  })

  it('hides a project with no sessions, so a lone empty project leaves no switcher', () => {
    setSidebarAgentsGrouped(true)
    $projectTree.set([{ ...realProject, isAuto: false, sessionCount: 0 }])
    $sessions.set(sessionRows)

    renderSidebar('/', 'chat')

    expect(screen.queryByText('new-project')).toBeNull()
    expect(screen.queryByText('Projects')).toBeNull()
  })

  it('keeps the "Sessions" label when grouping is on but no projects exist', () => {
    // The source-filter toggle with an empty tree: the section lists plain
    // chat sessions, so heading it "Projects" mislabeled them (#62537).
    setSidebarAgentsGrouped(true)
    $projectTree.set([])
    $sessions.set(sessionRows)

    renderSidebar('/', 'chat')

    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(screen.queryByText('Projects')).toBeNull()
  })

  it('keeps the "Sessions" label when the grouped tree is only the synthetic Home bucket', () => {
    // No real projects — every session fell into Home. A lone Home bucket is
    // the flat session list wearing a project costume, not a switcher.
    setSidebarAgentsGrouped(true)
    $projectTree.set(homeOnly)
    $sessions.set(sessionRows)

    renderSidebar('/', 'chat')

    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(screen.queryByText('Projects')).toBeNull()
  })
})

describe('ChatSidebar empty-profile sections', () => {
  // A profile with no normal sessions but messaging threads / cron jobs must
  // still show them: the session area (and the sections inside it) is gated by
  // showSessionSections, which used to key only on normal-session visibility
  // and collapsed these profiles to the blank state (#63593).
  const telegramThread = (id: string, last_active: number) =>
    makeSessionInfo({ connection_id: 'local', id, last_active, profile: 'default', source: 'telegram', title: id })

  beforeEach(() => {
    $connectionsRegistry.set({
      version: 2,
      primary: 'local',
      secureTokenStorage: true,
      connections: [{ id: 'local', label: 'This computer', kind: 'local', tokenSet: false, tokenPreview: null }]
    } as NonNullable<typeof $connectionsRegistry.value>)
    $profiles.set([{ name: 'default', is_default: true }] as typeof $profiles.value)
    $sessions.set([])
    $messagingSessions.set([telegramThread('tg-one', 60), telegramThread('tg-two', 30)])
    $messagingTruncated.set(false)
    $sidebarMessagingOpenIds.set(['telegram'])
    $sessionsLoading.set(false)
  })

  afterEach(() => {
    cleanup()
    $messagingSessions.set([])
    $sidebarMessagingOpenIds.set([])
    $sessions.set([])
  })

  it('shows the messaging section for a profile with no normal sessions', () => {
    const { container } = renderSidebar('/', 'chat')

    // The Telegram platform section renders with its threads…
    expect(screen.getAllByText('Telegram').length).toBeGreaterThan(0)
    expect(screen.getByText('tg-one')).toBeTruthy()
    // The session area still renders even though search is now disclosed only
    // by its action, not used as an always-visible area sentinel.
    expect(container.querySelector('[data-sessions-mode]')).not.toBeNull()
    expect(screen.queryByPlaceholderText('Search sessions…')).toBeNull()
  })

  it('still shows the blank state when a profile has nothing at all', () => {
    $messagingSessions.set([])
    $sidebarMessagingOpenIds.set([])

    // SidebarBlankState: a centered "No sessions yet" plus a "New project"
    // button, rendered INSTEAD of the whole session area (search included).
    const { container } = renderSidebar('/', 'chat')

    expect(container.textContent).toContain('No sessions yet')
    expect(screen.queryByPlaceholderText('Search sessions…')).toBeNull()
  })

  it('shows a section per profile when several profiles have no chats anywhere', () => {
    $messagingSessions.set([])
    $sidebarMessagingOpenIds.set([])
    $profiles.set([
      { name: 'default', is_default: true },
      { name: 'work', is_default: false }
    ] as typeof $profiles.value)
    setShowAllProfiles(true)
    setSidebarGrouping('profile')

    try {
      const { container } = renderSidebar('/', 'chat')

      const groups = [...container.querySelectorAll<HTMLElement>('[data-gateway-group]')].map(
        node => node.dataset.gatewayGroup
      )

      expect(container.querySelector('[data-sessions-mode]')).not.toBeNull()
      expect(screen.queryByPlaceholderText('Search sessions…')).toBeNull()
      expect(groups.some(id => id?.includes('"default"'))).toBe(true)
      expect(groups.some(id => id?.includes('"work"'))).toBe(true)
    } finally {
      setSidebarGrouping('date')
      setShowAllProfiles(false)
    }
  })
})

describe('ChatSidebar messaging owners', () => {
  const telegram = (id: string, profile: string, last_active: number) =>
    makeSessionInfo({ connection_id: 'local', id, last_active, profile, source: 'telegram', title: id })

  // Newest first: the 3-row cap lets default-1, work-1 and default-2 through.
  const threads = [
    telegram('default-1', 'default', 50),
    telegram('work-1', 'work', 40),
    telegram('default-2', 'default', 30),
    telegram('work-2', 'work', 20),
    telegram('default-3', 'default', 10)
  ]

  let root: HTMLElement

  const mount = () => {
    root = renderSidebar('/', 'chat').container
  }

  const telegramGroups = () =>
    [...root.querySelectorAll<HTMLElement>('[data-gateway-group]')].filter(node =>
      node.dataset.gatewayGroup!.startsWith(JSON.stringify(['messaging:telegram']).slice(0, -1))
    )

  const titlesIn = (node: HTMLElement) =>
    threads.map(thread => thread.id).filter(title => within(node).queryByText(title))

  beforeEach(() => {
    $connectionsRegistry.set({
      version: 2,
      primary: 'local',
      secureTokenStorage: true,
      connections: [{ id: 'local', label: 'This computer', kind: 'local', tokenSet: false, tokenPreview: null }]
    } as NonNullable<typeof $connectionsRegistry.value>)
    $profiles.set([
      { name: 'default', is_default: true },
      { name: 'work', is_default: false }
    ] as typeof $profiles.value)
    $sessions.set([
      makeSessionInfo({ connection_id: 'local', id: 'desk', last_active: 60, profile: 'default', title: 'desk' })
    ])
    $messagingSessions.set(threads)
    $messagingTruncated.set(false)
    $sidebarMessagingOpenIds.set(['telegram'])
  })

  afterEach(() => {
    cleanup()
    setSidebarGrouping('date')
    setShowAllProfiles(false)
    $activeGatewayProfile.set('default')
    $gatewayGroupCollapsed.set([])
    $sidebarMessagingOpenIds.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $profiles.set([])
    $connectionsRegistry.set(null)
  })

  it('groups the capped rows by owner, pages the platform as a whole, and keeps its own collapse keys', () => {
    setSidebarGrouping('profile')
    mount()

    const [defaultGroup, workGroup] = telegramGroups()

    expect(telegramGroups()).toHaveLength(2)
    expect(titlesIn(defaultGroup)).toEqual(['default-1', 'default-2'])
    expect(titlesIn(workGroup)).toEqual(['work-1'])

    fireEvent.click(screen.getByRole('button', { name: 'Load 2 more' }))

    expect(titlesIn(telegramGroups()[0])).toEqual(['default-1', 'default-2', 'default-3'])
    expect(titlesIn(telegramGroups()[1])).toEqual(['work-1', 'work-2'])
    expect(screen.queryByRole('button', { name: /^Load \d+ more$/ })).toBeNull()

    fireEvent.click(within(telegramGroups()[0]).getByRole('button', { name: 'Hide default sessions' }))

    expect(screen.queryByText('default-1')).toBeNull()
    expect(screen.getByText('desk')).toBeTruthy()
  })

  it('tags rows with their profile under other groupings and stays flat when scoped to one profile', () => {
    setShowAllProfiles(true)
    setSidebarGrouping('date')
    mount()

    const row = (title: string) => screen.getByText(title).closest('.group.row-hover') as HTMLElement

    expect(telegramGroups()).toHaveLength(0)
    expect(within(row('work-1')).getByRole('img', { name: 'Profile: work' })).toBeTruthy()

    cleanup()
    setSidebarGrouping('profile')
    setShowAllProfiles(false)
    mount()

    expect(telegramGroups()).toHaveLength(0)
    expect(screen.queryByText('work-1')).toBeNull()
    expect(within(row('default-1')).queryByRole('img', { name: /^Profile:/ })).toBeNull()
  })
})
