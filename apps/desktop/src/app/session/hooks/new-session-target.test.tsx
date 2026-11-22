import { useStore } from '@nanostores/react'
import { act, cleanup, render, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarDraftRows } from '@/app/chat/sidebar/draft-rows'
import {
  NO_PROJECT_ID,
  overlayLiveLanes,
  overlayLivePreviews,
  sessionBucketId,
  sessionMatchesProjectFilter,
  type SidebarProjectTree
} from '@/app/chat/sidebar/projects/workspace-groups'
import { $mainDraftSessionCwd } from '@/app/session/new-session-cwd'
import { notifyNewSessionTargetUnreachable, prepareDefaultNewSession } from '@/app/session/new-session-route'
import { group } from '@/components/pane-shell/tree/model'
import { $activeTreeGroup, $layoutTree } from '@/components/pane-shell/tree/store'
import type { DesktopProfileRoute } from '@/global'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionsRegistry } from '@/store/connections'
import { $defaultProfileRoute, setDefaultProfile } from '@/store/default-profile'
import { activeGatewayConnectionId, requestGatewayForAgent } from '@/store/gateway'
import { wipeSessionListsForGatewaySwitch } from '@/store/gateway-switch'
import { $notifications, clearNotifications } from '@/store/notifications'
import {
  $activeGatewayProfile,
  $newChatConnectionId,
  $newChatProfile,
  $newChatRoute,
  captureNewChatSource,
  ensureGatewayAgent
} from '@/store/profile'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import { $projects, followEnteredProjectCwd, resolveNewSessionCwd } from '@/store/projects'
import {
  $currentCwd,
  $newChatWorkspaceTarget,
  $sessions,
  setFreshDraftReady,
  setNewChatWorkspaceTarget,
  setSelectedStoredSessionId
} from '@/store/session'
import { $unlistedSessionOwnerRows } from '@/store/session'
import {
  _resetSessionOwnerHintsForTests,
  applyConfiguredDefaultProjectDir,
  setActiveSessionId,
  setConnection,
  setSessions
} from '@/store/session'
import { setCurrentCwd } from '@/store/session'
import { $sessionTiles, closeAllOpenSessionTiles } from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

import { useCwdActions } from './use-cwd-actions'
import { useSessionActions } from './use-session-actions'

// [daveey] "New sessions on <gateway>": the saved default route (device
// switcher → right-click → "Use for new sessions") decides where a generic new
// session is created, no matter which gateway's session is on screen.

vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(async () => undefined),
  ensureGatewayProfile: vi.fn(async () => undefined)
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  activeGatewayConnectionId: vi.fn(() => 'local'),
  requestGatewayForAgent: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

const HUB = { connectionId: 'hermes-hub', profile: 'default' }
const FOREGROUND_CREATE_DIAL = [undefined, undefined, { spawnPriority: 'foreground' }] as const

// Oracle copied verbatim from the pre-PR send path, independent of display selectors.
function prePrSendCwd() {
  const workspaceTarget = $newChatWorkspaceTarget.get()
  const homeScope = $projectScope.get() === NO_PROJECT_ID

  return workspaceTarget === null || (workspaceTarget === undefined && homeScope)
    ? ''
    : typeof workspaceTarget === 'string'
      ? workspaceTarget.trim()
      : $currentCwd.get().trim() || resolveNewSessionCwd()
}

function mountActions() {
  const ref = <T,>(current: T) => ({ current })
  const state = createClientSessionState()

  return renderHook(() =>
    useSessionActions({
      activeSessionId: null,
      activeSessionIdRef: ref<string | null>(null),
      busyRef: ref(false),
      creatingSessionRef: ref(false),
      ensureSessionState: () => state,
      getRouteToken: () => 'route',
      getRoutedStoredSessionId: () => null,
      navigate: vi.fn(),
      requestGateway: vi.fn(async () => ({ session_id: 'ambient', stored_session_id: 'ambient-stored' }) as never),
      resetViewSync: vi.fn(),
      routedSessionId: null,
      runtimeIdByStoredSessionIdRef: ref(new Map()),
      selectedStoredSessionId: null,
      selectedStoredSessionIdRef: ref<string | null>(null),
      sessionStateByRuntimeIdRef: ref(new Map()),
      syncSessionStateToView: vi.fn(),
      updateSessionState: () => state
    })
  )
}

beforeEach(() => {
  _resetSessionOwnerHintsForTests()
  $defaultProfileRoute.set(null)
  $newChatRoute.set(null)
  $newChatProfile.set(null)
  $activeGatewayProfile.set('default')
  $projectScope.set(ALL_PROJECTS)
  $connectionsRegistry.set({
    connections: [
      { id: 'local', kind: 'local', label: 'This device', tokenPreview: null, tokenSet: false },
      { id: 'mac-mini', kind: 'ssh', label: 'mac-mini', tokenPreview: null, tokenSet: false },
      { id: 'hermes-hub', kind: 'ssh', label: 'hub (EC2)', tokenPreview: null, tokenSet: false }
    ],
    primary: 'local',
    secureTokenStorage: true,
    version: 2
  } as never)
  setSessions([])
  setActiveSessionId(null)
  setConnection({
    baseUrl: 'http://localhost:7070',
    connectionId: 'local',
    isFullscreen: false,
    logs: [],
    mode: 'local',
    nativeOverlayWidth: 0,
    token: '',
    windowButtonPosition: null,
    wsUrl: 'ws://localhost:7070'
  })
  window.hermesDesktop = {
    api: vi.fn(async () => ({ messages: [], total: 0 })),
    profile: { setDefault: async (route: DesktopProfileRoute) => route }
  } as never
  vi.mocked(requestGatewayForAgent).mockReset()
  vi.mocked(requestGatewayForAgent).mockResolvedValue({
    session_id: 'created',
    stored_session_id: 'created-stored',
    info: {}
  })
})

afterEach(() => {
  cleanup()
  applyConfiguredDefaultProjectDir('')
  window.history.replaceState(null, '', '/')
  $defaultProfileRoute.set(null)
  $newChatRoute.set(null)
  $newChatProfile.set(null)
  $newChatConnectionId.set(null)
  $connectionsRegistry.set(null)
  captureNewChatSource(null)
  setActiveSessionId(null)
  setConnection(null)
  setSessions([])
  clearNotifications()
  vi.clearAllMocks()
})

describe('new-session gateway target', () => {
  it.each(['enter project after tile', 'pick same tile folder', 'choose same cwd', 'return to tile cwd', 'close tile'])(
    'preserves main workspace intent after %s',
    async operation => {
      const { closeSessionTile } = await import('@/store/session-states')
      const { result } = mountActions()
      await act(() => setDefaultProfile(HUB))
      act(() => {
        $projects.set([
          { id: 'original', name: 'Original', folders: [{ path: '/repo' }] },
          { id: 'next', name: 'Next', folders: [{ path: '/new-project' }] },
          { id: 'tile', name: 'Tile', folders: [{ path: '/other-project' }] }
        ] as never)
        $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'boundary' }))
        $activeTreeGroup.set('boundary')
        setSelectedStoredSessionId(null)
        result.current.startFreshSessionDraft()
        followEnteredProjectCwd('/repo')
      })
      vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
        session_id: 'tile-runtime',
        stored_session_id: 'tile-stored',
        info: { cwd: '/other-project' }
      })
      await act(() => result.current.openNewSessionTile('center', { cwd: '/other-project', listed: false, route: HUB }))
      act(() => {
        $layoutTree.set(group(['workspace', 'session-tile:tile-stored'], { active: 'workspace', id: 'boundary' }))
        $activeTreeGroup.set('boundary')

        if (operation === 'close tile') {
          closeSessionTile('tile-stored')
        } else if (operation === 'pick same tile folder') {
          setNewChatWorkspaceTarget('/other-project')
        } else if (operation === 'choose same cwd') {
          setCurrentCwd('/other-project')
        } else if (operation === 'return to tile cwd') {
          followEnteredProjectCwd('/new-project')
          followEnteredProjectCwd('/other-project')
        } else {
          followEnteredProjectCwd('/new-project')
        }
      })
      const expectedCwd = operation === 'enter project after tile' ? '/new-project' : '/other-project'

      const expectedBucket =
        operation === 'close tile' ? 'original' : operation === 'enter project after tile' ? 'next' : 'tile'

      const view = render(
        <MemoryRouter>
          <SidebarDraftRows projectId="original" />
          <SidebarDraftRows projectId="next" />
          <SidebarDraftRows projectId="tile" />
        </MemoryRouter>
      )

      expect
        .soft(view.container.querySelector('[data-sidebar-draft-project]')?.getAttribute('data-sidebar-draft-project'))
        .toBe(expectedBucket)
      const beforeSendCwd = prePrSendCwd()
      await act(() => result.current.createBackendSessionForSend('First send'))
      const call = vi.mocked(requestGatewayForAgent).mock.calls.findLast(call => call[2] === 'session.create')!
      expect(call[3]?.cwd).toBe(expectedCwd)
      expect(call[3]?.cwd || '').toBe(beforeSendCwd)
      expect(sessionBucketId($sessions.get()[0], $projects.get())).toBe(
        operation === 'close tile' ? 'tile' : expectedBucket
      )
      expect(view.container.querySelectorAll('[data-sidebar-draft-id]')).toHaveLength(0)
      act(() => {
        $projects.set([])
        closeAllOpenSessionTiles('workspace')
      })
    }
  )
  it.each([
    'default seed',
    'gateway and seed',
    'normalized project',
    'normalized owned project',
    'remote default',
    'background cwd',
    'detached',
    'Home scope',
    'tile workspace'
  ])('keeps draft placement identical to the real first send after %s', async operation => {
    const { startWorkspaceSession } = await import('@/app/session/workspace-session-target')
    const { ensureDefaultWorkspaceCwd, setNewChatWorkspaceTarget } = await import('@/store/session')
    const { result } = mountActions()
    await act(() => setDefaultProfile(HUB))
    act(() => {
      $projects.set([{ id: 'p1', name: 'Project', folders: [{ path: '/repo' }] }] as never)
      $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'invariant' }))
      $activeTreeGroup.set('invariant')
      setSelectedStoredSessionId(null)
      setCurrentCwd('')
      result.current.startFreshSessionDraft()
    })

    const view = render(
      <MemoryRouter>
        <SidebarDraftRows projectId="p1" />
        <SidebarDraftRows projectId={NO_PROJECT_ID} />
      </MemoryRouter>
    )

    const placement = () =>
      view.container.querySelector('[data-sidebar-draft-id]')?.getAttribute('data-sidebar-draft-project')

    if (operation.includes('seed')) {
      window.hermesDesktop = {
        ...window.hermesDesktop,
        sanitizeWorkspaceCwd: async (cwd: string) => ({ cwd }),
        settings: { getDefaultProjectDir: async () => ({ dir: '/repo', defaultLabel: '', resolvedCwd: '/repo' }) }
      } as never

      if (operation === 'gateway and seed') {
        act(() => wipeSessionListsForGatewaySwitch())
      }

      await act(() => ensureDefaultWorkspaceCwd())
    } else if (operation.startsWith('normalized')) {
      vi.mocked(requestGatewayForAgent).mockImplementation(async (_connection, _profile, method) =>
        method === 'config.get'
          ? { cwd: '/repo' }
          : { session_id: 'created', stored_session_id: 'created-stored', info: {} }
      )
      await act(async () =>
        startWorkspaceSession({
          path: '/link',
          ...(operation === 'normalized owned project' ? { owner: HUB } : {}),
          activeSessionIdRef: { current: null },
          requestGateway: vi.fn(async () => ({ cwd: '/repo' })) as never,
          startFreshSessionDraft: result.current.startFreshSessionDraft,
          followActiveSessionCwd: vi.fn()
        })
      )
    } else if (operation === 'tile workspace') {
      act(() => followEnteredProjectCwd('/repo'))
      vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
        session_id: 'tile-runtime',
        stored_session_id: 'tile-stored',
        info: { cwd: '/other-project' }
      })
      await act(() => result.current.openNewSessionTile('center', { cwd: '/other-project', listed: false, route: HUB }))
      act(() => {
        $layoutTree.set(group(['workspace', 'session-tile:tile-stored'], { active: 'workspace', id: 'invariant' }))
        $activeTreeGroup.set('invariant')
      })
    } else {
      act(() => {
        setCurrentCwd('/repo')

        if (operation === 'detached') {
          setNewChatWorkspaceTarget(null)
        }

        if (operation === 'Home scope') {
          $projectScope.set(NO_PROJECT_ID)
        }
      })
    }

    const expectedCwd = operation === 'detached' || operation === 'Home scope' ? '' : '/repo'
    const expectedBucket = expectedCwd ? 'p1' : NO_PROJECT_ID
    expect(placement()).toBe(expectedBucket)
    expect(view.container.querySelectorAll('[data-sidebar-draft-id]')).toHaveLength(1)
    const beforeSendCwd = prePrSendCwd()
    await act(() => result.current.createBackendSessionForSend('First send'))
    const createCall = vi.mocked(requestGatewayForAgent).mock.calls.findLast(call => call[2] === 'session.create')!
    expect(createCall[3]?.cwd || '').toBe(beforeSendCwd)
    expect(beforeSendCwd).toBe(operation === 'tile workspace' ? '/other-project' : expectedCwd)
    expect($sessions.get()).toHaveLength(1)
    expect(sessionBucketId($sessions.get()[0], $projects.get())).toBe(
      sessionBucketId(makeSessionInfo({ cwd: beforeSendCwd }), $projects.get())
    )
    expect(view.container.querySelectorAll('[data-sidebar-draft-id]')).toHaveLength(0)
    act(() => {
      $projects.set([])
      closeAllOpenSessionTiles('workspace')
    })
  })

  it.each(['enter project', 'pick folder', 'switch gateway'])(
    'keeps the main draft placement current after %s',
    async operation => {
      const { result } = mountActions()

      const cwdActions = renderHook(() =>
        useCwdActions({
          activeSessionIdRef: { current: null },
          requestGateway: vi.fn(async () => ({ cwd: '/repo/normalized' })) as never
        })
      )

      act(() => {
        $projects.set([{ id: 'p1', name: 'Project', folders: [{ path: '/repo' }] }] as never)
        $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'draft-placement' }))
        $activeTreeGroup.set('draft-placement')
        setSelectedStoredSessionId(null)
        setCurrentCwd('')
        result.current.startFreshSessionDraft(operation === 'switch gateway' ? { workspaceTarget: '/repo' } : undefined)
      })

      const view = render(
        <MemoryRouter>
          <SidebarDraftRows projectId="p1" />
          <SidebarDraftRows projectId={NO_PROJECT_ID} />
        </MemoryRouter>
      )

      const placement = () =>
        view.container.querySelector('[data-sidebar-draft-id]')?.getAttribute('data-sidebar-draft-project')

      expect(placement()).toBe(operation === 'switch gateway' ? 'p1' : NO_PROJECT_ID)

      if (operation === 'enter project') {
        act(() => followEnteredProjectCwd('/repo'))
      } else if (operation === 'pick folder') {
        await act(() => cwdActions.result.current.changeSessionCwd('/repo'))
      } else {
        act(() => wipeSessionListsForGatewaySwitch())
      }

      expect(view.container.querySelectorAll('[data-sidebar-draft-id]')).toHaveLength(1)
      expect(placement()).toBe('p1')
      expect($mainDraftSessionCwd.get()).toBe(operation === 'pick folder' ? '/repo/normalized' : '/repo')

      if (operation !== 'switch gateway') {
        await act(() => result.current.createBackendSessionForSend('First send'))
        const row = $sessions.get().find(row => row.id === 'created-stored')!
        expect(sessionBucketId(row, $projects.get())).toBe('p1')
        expect(row.cwd).toBe(operation === 'pick folder' ? '/repo/normalized' : '/repo')
        expect(view.container.querySelectorAll('[data-sidebar-draft-id]')).toHaveLength(0)
      }

      act(() => {
        $projects.set([])
        setFreshDraftReady(false)
      })
    }
  )

  it.each(['main', 'tab'])(
    'replaces a real %s draft with exactly one hub Home row before any tree refresh',
    async kind => {
      const old = makeSessionInfo({ id: 'older', last_active: 1, message_count: 1 })

      const home: SidebarProjectTree = {
        id: NO_PROJECT_ID,
        isNoProject: true,
        label: 'Home',
        path: null,
        repos: [
          {
            id: NO_PROJECT_ID,
            label: 'Home',
            path: null,
            sessionCount: 1,
            groups: [{ id: NO_PROJECT_ID, label: 'Home', path: null, sessions: [old] }]
          }
        ],
        previewSessions: [old],
        sessionIds: ['older'],
        sessionCount: 1
      }

      function SidebarList() {
        const sessions = useStore($sessions)
        const preview = overlayLivePreviews([home], sessions, [], 10)[NO_PROJECT_ID]

        return (
          <MemoryRouter>
            <SidebarDraftRows projectId={NO_PROJECT_ID} />
            {preview.map(row => (
              <div data-real-row={row.id} key={row.id}>
                {row.preview || row.id}
              </div>
            ))}
          </MemoryRouter>
        )
      }

      const { result } = mountActions()
      await act(() => setDefaultProfile(HUB))
      act(() => {
        $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'draft-test' }))
        $activeTreeGroup.set('draft-test')
        $newChatRoute.set(HUB)
        setActiveSessionId(null)
        setSelectedStoredSessionId(null)
        setFreshDraftReady(false)
        setSessions([old])
        $projectScope.set(NO_PROJECT_ID)
      })

      if (kind === 'main') {
        act(() => result.current.startFreshSessionDraft({ workspaceTarget: null }))
      } else {
        await act(() => result.current.openNewSessionTile('center', { cwd: null, listed: false, route: HUB }))
        // The shell's pane registry adopts a newly created tile asynchronously.
        act(() => {
          $layoutTree.set(
            group(['workspace', 'session-tile:created-stored'], {
              active: 'session-tile:created-stored',
              id: 'draft-test'
            })
          )
          $activeTreeGroup.set('draft-test')
        })
      }

      render(<SidebarList />)
      const painted = () => [...document.querySelectorAll('[data-sidebar-draft-id], [data-real-row]')]
      expect(
        painted(),
        JSON.stringify({
          tiles: $sessionTiles.get(),
          stubs: $unlistedSessionOwnerRows.get(),
          notifications: $notifications.get()
        })
      ).toHaveLength(2)
      expect(painted()[0].hasAttribute('data-sidebar-draft-id')).toBe(true)
      expect($sessions.get().map(row => row.id)).toEqual(['older'])

      if (kind === 'main') {
        await act(() => result.current.createBackendSessionForSend('First send'))
      } else {
        const { listTileSessionRow } = await import('@/app/chat/session-tile-actions')
        act(() => {
          listTileSessionRow({
            cwd: '',
            preview: 'First send',
            runtimeId: 'created',
            storedSessionId: 'created-stored',
            sessions: $sessions.get()
          })
        })
      }

      expect(painted()).toHaveLength(2)
      expect(painted()[0].getAttribute('data-real-row')).toBe('created-stored')
      const row = $sessions.get()[0]
      expect(row.connection_id).toBe(HUB.connectionId)
      expect(row.cwd || null).toBeNull()
      expect(sessionBucketId(row, [])).toBe(NO_PROJECT_ID)
      expect(sessionMatchesProjectFilter(row, [NO_PROJECT_ID], [])).toBe(true)
      expect(overlayLiveLanes(home, $sessions.get()).repos[0].groups[0].sessions.map(s => s.id)).toEqual([
        'created-stored',
        'older'
      ])
      expect(home.previewSessions?.map(s => s.id)).toEqual(['older'])
      expect(requestGatewayForAgent).toHaveBeenCalledWith(
        HUB.connectionId,
        HUB.profile,
        'session.create',
        expect.anything(),
        ...FOREGROUND_CREATE_DIAL
      )
      act(() => {
        closeAllOpenSessionTiles('workspace')
        $sessionTiles.set([])
        $unlistedSessionOwnerRows.set([])
        setFreshDraftReady(false)
      })
    }
  )

  it('opening a session on another gateway leaves new sessions on the saved gateway', async () => {
    const { result } = mountActions()
    await act(() => setDefaultProfile(HUB))

    // A click on a mac-mini session: resume through its owner route, which
    // brings mac-mini to the foreground (the ambient source new sessions used
    // to follow).
    vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
      session_id: 'mini-runtime',
      stored_session_id: 'mini-stored',
      info: {},
      messages: []
    })
    await act(() =>
      result.current.resumeSession('mini-stored', false, { connectionId: 'mac-mini', profile: 'default' })
    )
    vi.mocked(activeGatewayConnectionId).mockReturnValue('mac-mini')

    expect($defaultProfileRoute.get()).toEqual(HUB)

    act(() => result.current.selectSidebarItem({ action: 'new-session' } as never))
    await act(() => result.current.createBackendSessionForSend())

    expect(requestGatewayForAgent).toHaveBeenLastCalledWith(
      'hermes-hub',
      'default',
      'session.create',
      expect.objectContaining({ profile: 'default' }),
      ...FOREGROUND_CREATE_DIAL
    )
  })

  it('without a saved gateway a new session still follows the gateway on screen (upstream)', async () => {
    vi.mocked(activeGatewayConnectionId).mockReturnValue('mac-mini')
    const { result } = mountActions()

    act(() => result.current.selectSidebarItem({ action: 'new-session' } as never))
    await act(() => result.current.createBackendSessionForSend())

    expect(requestGatewayForAgent).toHaveBeenLastCalledWith(
      'mac-mini',
      'default',
      'session.create',
      expect.anything(),
      ...FOREGROUND_CREATE_DIAL
    )
  })

  it('an unreachable saved gateway keeps the draft there and offers This device for this session only', async () => {
    await act(() => setDefaultProfile(HUB))
    vi.mocked(ensureGatewayAgent).mockRejectedValueOnce(new Error('ssh: connect to host hub: timed out'))

    act(() => prepareDefaultNewSession())
    await vi.waitFor(() => expect($notifications.get()).toHaveLength(1))

    const [notice] = $notifications.get()
    expect(notice).toMatchObject({ kind: 'warning', detail: 'ssh: connect to host hub: timed out' })
    expect(notice.title).toContain('hub (EC2)')
    // No silent fallback: the draft is still owned by the hub.
    expect($newChatRoute.get()).toEqual(HUB)
    expect(ensureGatewayAgent).not.toHaveBeenCalledWith('local', expect.anything())

    act(() => notice.action?.onClick())

    expect($newChatRoute.get()).toEqual({ connectionId: 'local', profile: 'default' })
    expect($defaultProfileRoute.get()).toEqual(HUB)
  })

  it('offers no fallback when the saved gateway is This device itself', () => {
    notifyNewSessionTargetUnreachable({ connectionId: 'local', profile: 'default' }, new Error('down'))

    expect($notifications.get()[0]?.action).toBeUndefined()
  })
})
