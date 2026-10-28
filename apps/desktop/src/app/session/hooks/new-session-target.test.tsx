import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { notifyNewSessionTargetUnreachable, prepareDefaultNewSession } from '@/app/session/new-session-route'
import type { DesktopProfileRoute } from '@/global'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionsRegistry } from '@/store/connections'
import { $defaultProfileRoute, setDefaultProfile } from '@/store/default-profile'
import { activeGatewayConnectionId, requestGatewayForAgent } from '@/store/gateway'
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
import {
  _resetSessionOwnerHintsForTests,
  applyConfiguredDefaultProjectDir,
  setActiveSessionId,
  setConnection,
  setSessions
} from '@/store/session'

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
