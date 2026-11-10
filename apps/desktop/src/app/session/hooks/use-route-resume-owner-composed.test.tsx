import { useStore } from '@nanostores/react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import { getLatestSessionMessages } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import * as gateways from '@/store/gateway'
import { $activeGatewayProfile, $showAllProfiles } from '@/store/profile'
import {
  $activeSessionId,
  $messages,
  $selectedStoredSessionId,
  _resetSessionOwnerHintsForTests,
  setActiveSessionId,
  setConnection,
  setMessages,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import { $sessionStates, clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

import type { ClientSessionState } from '../../types'

import { useRouteResume } from './use-route-resume'
import { useSessionActions } from './use-session-actions'

vi.mock('@/app/contrib/hooks/use-background-sync', () => ({ resetLiveRuntimeTracking: vi.fn() }))
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn(async () => ({
    session_id: 'remote-session',
    messages: [{ id: 1, role: 'assistant', content: 'History from the remote owner', timestamp: 1 }]
  })),
  HermesGateway: class {
    connectionState = 'closed'
    connect = vi.fn(async () => {
      this.connectionState = 'open'
    })
    close = vi.fn(() => {
      this.connectionState = 'closed'
    })
    onEvent = () => () => undefined
    onState = () => () => undefined
    request = vi.fn(async (method: string) => {
      if (method !== 'session.resume') {
        throw new Error(`Unexpected remote RPC: ${method}`)
      }

      return {
        info: {},
        messages: [],
        messages_omitted: true,
        resumed: 'remote-session',
        session_id: 'remote-runtime',
        running: false
      }
    })
  }
}))

function descriptor(connectionId: string): HermesConnection {
  return {
    connectionId,
    profile: 'default',
    registryScoped: true,
    mode: connectionId === 'local' ? 'local' : 'remote',
    authMode: 'token',
    token: 'test',
    wsUrl: `ws://${connectionId}.invalid/ws`,
    baseUrl: `http://${connectionId}.invalid`
  } as HermesConnection
}

const local = { connectionState: 'closed', request: vi.fn(async () => ({}) as never) }
const ambientRequest = vi.fn(async () => ({}) as never)

function Harness({ requestSessionId = 'remote-session' }: { requestSessionId?: string }) {
  const states = useRef(new Map<string, ClientSessionState>())
  const activeSessionIdRef = useRef<string | null>(null)
  const selectedStoredSessionIdRef = useRef<string | null>(null)
  const creatingSessionRef = useRef(false)
  const busyRef = useRef(false)
  const runtimeIdByStoredSessionIdRef = useRef(new Map<string, string>())
  const activeSessionId = useStore($activeSessionId)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const messages = useStore($messages)

  const actions = useSessionActions({
    activeSessionId,
    activeSessionIdRef,
    busyRef,
    creatingSessionRef,
    ensureSessionState: (id, storedId) => {
      const state = states.current.get(id) ?? createClientSessionState(storedId ?? null)
      states.current.set(id, state)

      return state
    },
    getRouteToken: () => '/remote-session',
    getRoutedStoredSessionId: () => 'remote-session',
    navigate: vi.fn() as never,
    requestGateway: ambientRequest,
    resetViewSync: vi.fn(),
    routedSessionId: 'remote-session',
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef: states,
    syncSessionStateToView: (_id, state) => setMessages(state.messages),
    updateSessionState: (id, updater, storedId) => {
      const next = updater(states.current.get(id) ?? createClientSessionState(storedId ?? null))
      states.current.set(id, next)
      publishSessionState(id, next)

      return next
    }
  })

  useRouteResume({
    activeSessionId,
    activeSessionIdRef,
    creatingSessionRef,
    currentView: 'chat',
    freshDraftReady: false,
    // The old gateway's boot state never recovers during this open.
    gatewayState: local.connectionState,
    locationPathname: '/remote-session',
    resumeSession: actions.resumeSession,
    resumeFailedSessionId: null,
    resumeExhaustedSessionId: null,
    sessionResumeRequest: {
      sequence: 1,
      sessionId: requestSessionId,
      ownerRoute: { connectionId: 'remote', profile: 'default' }
    },
    routedSessionId: 'remote-session',
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef,
    startFreshSessionDraft: actions.startFreshSessionDraft
  })

  return <output data-testid="transcript">{JSON.stringify(messages)}</output>
}

describe('route resume composed with session actions and the gateway registry', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    clearAllSessionStates()
    _resetSessionOwnerHintsForTests()
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    $showAllProfiles.set(false)
    $activeGatewayProfile.set('default')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnectionFor: vi.fn(async ({ connectionId }: { connectionId: string }) => descriptor(connectionId)),
        getGatewayWsUrlFor: vi.fn(async ({ connectionId }: { connectionId: string }) => ({
          ok: true,
          wsUrl: `ws://${connectionId}.invalid/ws`
        }))
      }
    })
    gateways.configureGatewayRegistry({ onEvent: vi.fn(), onActiveRouteChanged: p => $activeGatewayProfile.set(p) })
    gateways.setPrimaryGateway(local as never, 'default')
    gateways.setPrimaryGatewayConnection(descriptor('local'))
    setConnection(descriptor('local'))
    setSessions([makeSessionInfo({ id: 'remote-session', profile: 'default', connection_id: 'remote', message_count: 1 })])
  })

  afterEach(() => {
    cleanup()
    gateways.closeSecondaryGateways()
    gateways.setPrimaryGateway(null)
    gateways.setPrimaryGatewayConnection(null)
    setConnection(null)
    setSessions([])
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    clearAllSessionStates()
    _resetSessionOwnerHintsForTests()
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.restoreAllMocks()
  })

  it('opens the remote owner and publishes its history while the local gateway stays closed', async () => {
    render(<Harness />)

    await waitFor(() => expect($activeSessionId.get()).toBe('remote-runtime'))
    const remote = gateways.activeGateway()!
    expect(gateways.activeGatewayConnectionId()).toBe('remote')
    expect(remote.request).toHaveBeenCalledExactlyOnceWith(
      'session.resume',
      expect.objectContaining({ session_id: 'remote-session' })
    )
    expect(remote.connectionState).toBe('open')
    expect(getLatestSessionMessages).toHaveBeenCalledWith('remote-session', {
      connectionId: 'remote',
      profile: 'default'
    })
    expect($selectedStoredSessionId.get()).toBe('remote-session')
    expect($sessionStates.get()['remote-runtime']).toMatchObject({
      storedSessionId: 'remote-session',
      messages: [expect.objectContaining({ parts: [expect.objectContaining({ text: 'History from the remote owner' })] })]
    })
    expect(screen.getByTestId('transcript').textContent).toContain('History from the remote owner')
    expect(local.request).not.toHaveBeenCalled()
    expect(ambientRequest).not.toHaveBeenCalled()
    expect(local.connectionState).toBe('closed')
  })

  it('does not use an owner request for a different session to bypass the closed gateway', async () => {
    const { rerender } = render(<Harness requestSessionId="another-session" />)
    expect(window.hermesDesktop!.getConnectionFor).not.toHaveBeenCalled()
    expect(getLatestSessionMessages).not.toHaveBeenCalled()
    expect($activeSessionId.get()).toBeNull()
    expect($sessionStates.get()).toEqual({})
    expect(screen.getByTestId('transcript').textContent).toBe('[]')
    expect(gateways.activeGateway()).toBe(local)
    expect(local.request).not.toHaveBeenCalled()
    expect(ambientRequest).not.toHaveBeenCalled()
    expect(local.connectionState).toBe('closed')

    // The ignored sequence must still be fresh once the request matches the route.
    rerender(<Harness />)
    await waitFor(() => expect($activeSessionId.get()).toBe('remote-runtime'))
    expect(gateways.activeGateway()!.request).toHaveBeenCalledExactlyOnceWith(
      'session.resume',
      expect.objectContaining({ session_id: 'remote-session' })
    )
    expect(local.request).not.toHaveBeenCalled()
    expect(ambientRequest).not.toHaveBeenCalled()
    expect(local.connectionState).toBe('closed')
  })
})
