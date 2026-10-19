/**
 * A session that lives on another registry gateway resumes on its OWNING
 * gateway, never the one the window happens to have selected — and a 4007
 * "session not found" from the wrong gateway follows the owner instead of
 * opening an empty chat.
 *
 * Observed live: with the Mac mini (local) gateway selected, opening MacBook
 * sessions sent `session.resume` to the mini, which (before its own fix)
 * fabricated an empty row — so the click painted an empty chat and the
 * selected gateway never moved to the MacBook.
 *
 * Every gateway is mocked per connection and per RPC method, so a resume that
 * lands on the wrong socket is observable rather than vacuously green.
 */
import { JsonRpcGatewayError } from '@hermes/shared'
import { cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages, getSession, type SessionInfo } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $activeConnectionId } from '@/store/connections'
import { openGatewayForAgent, requestGatewayForAgent, requestGatewayForProfile } from '@/store/gateway'
import { $showAllProfiles, ensureGatewayAgent, ensureGatewayProfile } from '@/store/profile'
import {
  $messages,
  $resumeFailedSessionId,
  $selectedStoredSessionId,
  _resetSessionOwnerHintsForTests,
  getSessionOwnerHint,
  setActiveSessionId,
  setConnection,
  setMessages,
  setResumeFailedSessionId,
  setSessions
} from '@/store/session'
import type { SessionProfileRoute } from '@/store/session-request-router'
import { clearAllSessionStates } from '@/store/session-states'

import type { ClientSessionState } from '../../types'

import { useSessionActions } from './use-session-actions'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  getSession: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(),
  ensureGatewayProfile: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  openGatewayForAgent: vi.fn(async () => undefined),
  openGatewayForProfile: vi.fn(async () => undefined),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

const MACBOOK = 'macbook'
const MACBOOK_SESSION = '20261013_184022_b1a59f'
const LOCAL_SESSION = '20261013_190000_aaaaaa'

type GatewayHandler = (method: string, params: Record<string, unknown>) => unknown

/** Per-connection, per-method gateway mocks. `local` is the window primary
 *  (the ambient socket); every other id is a registry connection reached
 *  through requestGatewayForAgent. */
function mockGateways(handlers: Record<string, GatewayHandler>, ambientConnection = 'local') {
  const calls: { connection: string; method: string; params: Record<string, unknown> }[] = []

  const dispatch = async (connection: string, method: string, params: Record<string, unknown> = {}) => {
    calls.push({ connection, method, params })
    const handler = handlers[connection]

    if (!handler) {
      throw new JsonRpcGatewayError(`no gateway for ${connection}`)
    }

    return handler(method, params)
  }

  const ambient = vi.fn(
    async (method: string, params?: Record<string, unknown>) => await dispatch(ambientConnection, method, params)
  ) as unknown as <T>(method: string, params?: Record<string, unknown>) => Promise<T>

  vi.mocked(requestGatewayForAgent).mockImplementation(async (connectionId, _profile, method, params) =>
    dispatch(connectionId === null ? 'local' : connectionId, method, params ?? {})
  )

  // The legacy profile door (a bare profile owner) is the primary's pool.
  vi.mocked(requestGatewayForProfile).mockImplementation(async (_profile, method, params) =>
    dispatch('local', method, params ?? {})
  )

  const resumesOn = (connection: string) =>
    calls.filter(call => call.connection === connection && call.method === 'session.resume')

  // A warm re-open binds through session.activate; a cold one through
  // session.resume. Either one on the wrong gateway is the misroute.
  const opensOn = (connection: string) =>
    calls.filter(
      call => call.connection === connection && (call.method === 'session.resume' || call.method === 'session.activate')
    )

  return { ambient, calls, opensOn, resumesOn }
}

/** A gateway that owns `ids` and answers 4007 for anything else — the
 *  post-fix backend contract for an id it never minted. */
function owningGateway(owned: string[], runtimeId: string): GatewayHandler {
  return (method, params) => {
    if (method === 'session.resume') {
      const id = String(params.session_id ?? '')

      if (!owned.includes(id)) {
        throw new JsonRpcGatewayError('session not found', { code: 4007 })
      }

      return {
        info: {},
        message_count: 2,
        messages: [],
        messages_omitted: true,
        resumed: id,
        session_id: runtimeId,
        session_key: id
      }
    }

    return {}
  }
}

function storedSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: MACBOOK_SESSION,
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'stored',
    tool_call_count: 0,
    ...overrides
  }
}

const TRANSCRIPT = {
  messages: [
    { content: 'hello from the macbook', role: 'user', timestamp: 1 },
    { content: 'hi', role: 'assistant', timestamp: 2 }
  ]
}

function ResumeHarness({
  onReady,
  requestGateway
}: {
  onReady: (
    resume: (storedSessionId: string, replaceRoute?: boolean, ownerRoute?: SessionProfileRoute) => Promise<unknown>
  ) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })
  const stateMapRef = ref(new Map<string, ClientSessionState>())

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway,
    resetViewSync: vi.fn(),
    routedSessionId: null,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: stateMapRef,
    holdSessionTranscriptView: () => () => undefined,
    syncSessionStateToView: vi.fn(),
    updateSessionState: (sessionId, updater, storedSessionId) => {
      const next = updater(stateMapRef.current.get(sessionId) ?? createClientSessionState(storedSessionId ?? null))
      stateMapRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

async function mountResume(requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>) {
  let resume: ((id: string, replaceRoute?: boolean, owner?: SessionProfileRoute) => Promise<unknown>) | null = null
  render(<ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} />)
  await waitFor(() => expect(resume).not.toBeNull())

  return resume!
}

describe('resume routes by the session owner, not the selected gateway', () => {
  beforeEach(() => {
    _resetSessionOwnerHintsForTests({ storage: true })
    // The window has the local (Mac mini) gateway selected.
    setConnection({ connectionId: 'local', mode: 'local', registryScoped: true } as never)

    // Activating a gateway publishes its descriptor, which is what the
    // connection switcher reads as the selected gateway.
    vi.mocked(ensureGatewayAgent).mockImplementation(async connectionId => {
      setConnection({ connectionId, mode: connectionId === 'local' ? 'local' : 'remote' } as never)
    })
    vi.mocked(ensureGatewayProfile).mockResolvedValue(undefined)
    vi.mocked(getLatestSessionMessages).mockResolvedValue({ ...TRANSCRIPT, session_id: MACBOOK_SESSION } as never)
    // The local backend never minted the MacBook id: by-id REST 404s there.
    vi.mocked(getSession).mockRejectedValue(new Error('404: Session not found'))
  })

  afterEach(() => {
    cleanup()
    clearAllSessionStates()
    setActiveSessionId(null)
    setResumeFailedSessionId(null)
    setMessages([])
    setSessions([])
    setConnection(null)
    $connectionsRegistry.set(null)
    $showAllProfiles.set(false)
    _resetSessionOwnerHintsForTests({ storage: true })
    vi.mocked(requestGatewayForAgent).mockReset()
    vi.mocked(ensureGatewayAgent).mockReset()
    vi.mocked(getSession).mockReset()
    vi.mocked(getLatestSessionMessages).mockReset()
  })

  it('(a) opening a row tagged with a foreign connection resumes ONLY there and selects that gateway', async () => {
    setSessions([storedSession({ connection_id: MACBOOK, profile: 'default' })])

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    // The sidebar row click: the route resume carries no explicit owner when
    // it was not an explicit request (navigation, ⌘⇧[ ], session switcher).
    await resume(MACBOOK_SESSION, true)

    expect(gateways.resumesOn('local')).toHaveLength(0)
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(1)
    expect($activeConnectionId.get()).toBe(MACBOOK)
    expect($selectedStoredSessionId.get()).toBe(MACBOOK_SESSION)
  })

  it('(a) a session reached by navigation keeps resuming on its owner after its row leaves the unified list', async () => {
    // ⌘⇧[ ] / the session switcher / a route change open the row with no
    // explicit owner. Later the MacBook's pooled backend reconnects and the
    // unified list briefly carries none of its rows (only pooled gateways are
    // read), while the selected (local) gateway re-opens and the route
    // re-resumes the visible session.
    setSessions([storedSession({ connection_id: MACBOOK, profile: 'default' })])

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(MACBOOK_SESSION, true)
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(1)

    // The user had the local gateway selected again by the time it reopened.
    setConnection({ connectionId: 'local', mode: 'local', registryScoped: true } as never)
    setSessions([])

    const before = gateways.opensOn(MACBOOK).length
    await resume(MACBOOK_SESSION, true)

    expect(gateways.opensOn('local')).toHaveLength(0)
    expect(gateways.opensOn(MACBOOK).length).toBeGreaterThan(before)
    expect($activeConnectionId.get()).toBe(MACBOOK)
  })

  it('(b) restoring a stored id whose only owner record is a foreign hint never resumes locally', async () => {
    // App start: the unified list has not spliced the MacBook rows yet (its
    // pooled backend is still dialing), so only the persisted hint knows.
    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    const { setSessionOwnerHint } = await import('@/store/session')
    setSessionOwnerHint(MACBOOK_SESSION, { connectionId: MACBOOK, profile: 'default' })

    await resume(MACBOOK_SESSION, true)

    expect(gateways.resumesOn('local')).toHaveLength(0)
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(1)
    expect($activeConnectionId.get()).toBe(MACBOOK)
  })

  it('(b) a tagged row in the unified list routes the restore even after a stale local hint', async () => {
    const { setSessionOwnerHint } = await import('@/store/session')
    setSessionOwnerHint(MACBOOK_SESSION, { connectionId: 'local', profile: 'default' })
    setSessions([storedSession({ connection_id: MACBOOK, profile: 'default' })])

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(MACBOOK_SESSION, true)

    expect(gateways.resumesOn('local')).toHaveLength(0)
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(1)
    expect(getSessionOwnerHint(MACBOOK_SESSION)).toMatchObject({ connectionId: MACBOOK })
  })

  it('(c) a 4007 from the wrong gateway re-routes the resume to the owner in the unified list', async () => {
    // The resume was dispatched with a stale explicit owner (`local`) — the
    // shape a takeover/4001 recovery or an old persisted hint produces —
    // while the unified list knows the MacBook owns the id.
    setSessions([storedSession({ connection_id: MACBOOK, profile: 'default' })])

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(MACBOOK_SESSION, true, { connectionId: 'local', profile: 'default' })

    await waitFor(() => expect(gateways.resumesOn(MACBOOK)).toHaveLength(1))
    expect($activeConnectionId.get()).toBe(MACBOOK)
    expect(getSessionOwnerHint(MACBOOK_SESSION)).toMatchObject({ connectionId: MACBOOK })
    // Not an empty chat and not a failure screen: the owner's transcript.
    await waitFor(() => expect($messages.get().length).toBeGreaterThan(0))
    expect($resumeFailedSessionId.get()).toBeNull()
  })

  it('(c) a 4007 for an id no connection owns keeps the existing not-found handling', async () => {
    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([], 'rt-macbook')
    })

    vi.mocked(getLatestSessionMessages).mockRejectedValue(new Error('404: Session not found'))

    const resume = await mountResume(gateways.ambient)
    await resume(MACBOOK_SESSION, true, { connectionId: 'local', profile: 'default' })

    // Nothing to follow: the existing not-found handling owns the outcome,
    // and the selected gateway is left where it was.
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(0)
    expect(gateways.resumesOn('local')).toHaveLength(1)
    expect($activeConnectionId.get()).toBe('local')
    expect(getSessionOwnerHint(MACBOOK_SESSION)).toBeUndefined()
  })

  it('(b) restoring an unlisted id with no owner record finds its connection before dispatching', async () => {
    // App start / reconnect re-resume: the MacBook rows are not in the unified
    // list yet and no hint survived. The selected gateway's store does not have
    // the id, so the owner is looked up across the registry — the resume must
    // never be sent to the local gateway first.
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: MACBOOK, kind: 'ssh', label: 'macbook' }
      ],
      primary: 'local',
      version: 2
    } as never)
    vi.mocked(getSession).mockImplementation(async (id, scope) => {
      if (id === MACBOOK_SESSION && typeof scope === 'object' && scope?.connectionId === MACBOOK) {
        return storedSession({ profile: 'default' })
      }

      throw new Error('404: Session not found')
    })

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(MACBOOK_SESSION, true)

    expect(gateways.resumesOn('local')).toHaveLength(0)
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(1)
    expect($activeConnectionId.get()).toBe(MACBOOK)
    expect(getSessionOwnerHint(MACBOOK_SESSION)).toMatchObject({ connectionId: MACBOOK })
  })

  it('(d) control: a local session still resumes on the local gateway', async () => {
    setSessions([storedSession({ id: LOCAL_SESSION, profile: 'default' })])
    vi.mocked(getLatestSessionMessages).mockResolvedValue({ ...TRANSCRIPT, session_id: LOCAL_SESSION } as never)

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(LOCAL_SESSION, true)

    expect(gateways.resumesOn(MACBOOK)).toHaveLength(0)
    expect(gateways.calls.filter(call => call.method === 'session.resume')).toHaveLength(1)
    expect(gateways.calls.find(call => call.method === 'session.resume')?.connection).toBe('local')
    expect($activeConnectionId.get()).toBe('local')
  })

  // Ghost-resume class [daveey]: with a remote gateway (nous-1) selected, the
  // unified list still carries this Mac's own chats. Electron now names their
  // owner (`local`) on the pinned aggregate; opening one must resume it here
  // and send nothing to the selected remote, which would materialize an empty
  // row for an id it never minted.
  it('(e) opening a local row while a remote gateway is selected resumes ONLY on local', async () => {
    const NOUS = 'skint-polycentrism'
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: NOUS, kind: 'remote', label: 'nous-1' }
      ],
      primary: 'local',
      version: 2
    } as never)
    setConnection({ connectionId: NOUS, mode: 'remote', registryScoped: true } as never)
    setSessions([storedSession({ connection_id: 'local', id: LOCAL_SESSION, profile: 'default' })])
    vi.mocked(getLatestSessionMessages).mockResolvedValue({ ...TRANSCRIPT, session_id: LOCAL_SESSION } as never)

    const gateways = mockGateways(
      {
        local: owningGateway([LOCAL_SESSION], 'rt-local'),
        [NOUS]: owningGateway([], 'rt-nous')
      },
      NOUS
    )

    const resume = await mountResume(gateways.ambient)
    await resume(LOCAL_SESSION, true)

    expect(gateways.calls.filter(call => call.connection === NOUS)).toEqual([])
    expect(gateways.resumesOn('local')).toHaveLength(1)
    expect($activeConnectionId.get()).toBe('local')
  })

  // The statusbar gateway switcher (use-statusbar-items.tsx StatusbarGatewaySwitcher
  // → connection-switcher.tsx) and the fleet profile rail ("All profiles on this
  // gateway") show the selected gateway in All-profiles mode too, so opening a
  // foreign gateway's session there must move it like the normal view does.
  it('(all-profiles) opening a foreign-gateway session switches the selected gateway, view stays All', async () => {
    $showAllProfiles.set(true)
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: MACBOOK, kind: 'ssh', label: 'macbook' }
      ],
      primary: 'local',
      version: 2
    } as never)
    setSessions([storedSession({ connection_id: MACBOOK, profile: 'default' })])

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(MACBOOK_SESSION, true)

    expect(gateways.resumesOn('local')).toHaveLength(0)
    expect(gateways.resumesOn(MACBOOK)).toHaveLength(1)
    expect($activeConnectionId.get()).toBe(MACBOOK)
    expect($showAllProfiles.get()).toBe(true)
  })

  it('(all-profiles) control: keeps the selected gateway for another profile on the same gateway', async () => {
    $showAllProfiles.set(true)
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: MACBOOK, kind: 'ssh', label: 'macbook' }
      ],
      primary: 'local',
      version: 2
    } as never)
    setSessions([storedSession({ connection_id: 'local', id: LOCAL_SESSION, profile: 'omar' })])
    vi.mocked(getLatestSessionMessages).mockResolvedValue({ ...TRANSCRIPT, session_id: LOCAL_SESSION } as never)

    const gateways = mockGateways({
      local: owningGateway([LOCAL_SESSION], 'rt-local'),
      [MACBOOK]: owningGateway([MACBOOK_SESSION], 'rt-macbook')
    })

    const resume = await mountResume(gateways.ambient)
    await resume(LOCAL_SESSION, true)

    expect(gateways.resumesOn(MACBOOK)).toHaveLength(0)
    expect(vi.mocked(ensureGatewayAgent)).not.toHaveBeenCalled()
    expect(vi.mocked(openGatewayForAgent)).toHaveBeenCalledWith('local', 'omar', expect.anything())
    expect($activeConnectionId.get()).toBe('local')
  })
})
