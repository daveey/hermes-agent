/**
 * Session-scoped RPCs (process.list, slash.exec, session.control.read,
 * session.activate, …) reach the gateway that OWNS the session, and a
 * not-found answer from a gateway that never held it follows the owner once
 * instead of polling the wrong backend forever.
 *
 * Observed live (Mac mini gui.log): with the mini (local) selected, MacBook
 * sessions' slash.exec / session.activate / process.list /
 * session.control.read hit the mini every 5s and were rejected "not in
 * memory" (4001), while a stale `local` owner record (hint / tile route)
 * outranked the MacBook-tagged row in the unified list.
 *
 * Every gateway is mocked per connection and per method, so an RPC that lands
 * on the wrong socket is observable rather than vacuously green.
 */
import { JsonRpcGatewayError } from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const gatewayMocks = vi.hoisted(() => ({
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn()
}))

vi.mock('@/store/gateway', async importActual => ({
  ...(await importActual<Record<string, unknown>>()),
  requestGatewayForAgent: gatewayMocks.requestGatewayForAgent,
  requestGatewayForProfile: gatewayMocks.requestGatewayForProfile,
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<Record<string, unknown>>()),
  getSession: vi.fn()
}))

const { getSession } = await import('@/hermes')
const { createSessionRpcDispatcher } = await import('./session-rpc-dispatcher')
const { $connectionsRegistry } = await import('@/store/connection-registry-state')
const { $profiles } = await import('@/store/profile')

const { _resetSessionOwnerHintsForTests, getSessionOwnerHint, setConnection, setSessionOwnerHint, setSessions } =
  await import('@/store/session')

const { $sessionTiles, clearAllSessionStates, requestForOwnedSession, sessionTileOwnerRoute } =
  await import('@/store/session-states')

const { _resetSessionOwnerFollowForTests } = await import('@/app/session/hooks/use-session-actions/owner-follow')
const { makeSessionInfo } = await import('@/test/session-info')

const MACBOOK = 'macbook'
const MACBOOK_SESSION = '20261013_184022_b1a59f'
const LOCAL_SESSION = '20261013_190000_aaaaaa'

type Call = { connection: string; method: string; params: Record<string, unknown> }

/** A gateway holding `stored` (and their runtime ids). Anything else answers
 *  4007 when addressed by a stored-shaped id, 4001 by a runtime id — the
 *  backend contract (`_sess_nowait` / `_resume_locate`). */
function gateway(held: Record<string, string>) {
  const runtimes = new Set(Object.values(held))

  return (method: string, params: Record<string, unknown>) => {
    const id = String(params.session_id ?? '')

    if (id && !(id in held) && !runtimes.has(id)) {
      throw /^\d{8}_\d{6}_/.test(id)
        ? new JsonRpcGatewayError('session not found', { code: 4007 })
        : new JsonRpcGatewayError('session not found', { code: 4001 })
    }

    return { method, ok: true }
  }
}

function mockGateways(handlers: Record<string, (method: string, params: Record<string, unknown>) => unknown>) {
  const calls: Call[] = []

  const dispatch = async (connection: string, method: string, params: Record<string, unknown> = {}) => {
    calls.push({ connection, method, params })

    return handlers[connection](method, params)
  }

  gatewayMocks.requestGatewayForAgent.mockImplementation(async (connectionId, _profile, method, params) =>
    dispatch(connectionId ?? 'local', method, params ?? {})
  )
  // The legacy profile door (a bare profile owner) is the primary's pool.
  gatewayMocks.requestGatewayForProfile.mockImplementation(async (_profile, method, params) =>
    dispatch('local', method, params ?? {})
  )

  const ambient = vi.fn(async (method: string, params?: Record<string, unknown>) =>
    dispatch('local', method, params ?? {})
  )

  return {
    ambient,
    calls,
    on: (connection: string) => calls.filter(call => call.connection === connection)
  }
}

function dispatcher(ambient: ReturnType<typeof vi.fn>, bindings: [string, string][] = []) {
  return createSessionRpcDispatcher({
    ambientRequest: ambient as never,
    runtimeIdByStoredSessionIdRef: { current: new Map(bindings) },
    selectedStoredSessionIdRef: { current: null },
    sessionStateByRuntimeIdRef: { current: new Map() }
  })
}

const handlers = () => ({
  local: gateway({ [LOCAL_SESSION]: 'rt-local' }),
  [MACBOOK]: gateway({ [MACBOOK_SESSION]: 'rt-mb' })
})

beforeEach(() => {
  _resetSessionOwnerFollowForTests()
  _resetSessionOwnerHintsForTests({ storage: true })
  $profiles.set([{ name: 'default' }] as never)
  $connectionsRegistry.set({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: MACBOOK, kind: 'ssh', label: 'macbook' }
    ],
    primary: 'local',
    version: 2
  } as never)
  // The window has the local (Mac mini) gateway selected.
  setConnection({ connectionId: 'local', mode: 'local', registryScoped: true } as never)
  vi.mocked(getSession).mockRejectedValue(new Error('404: Session not found'))
})

afterEach(() => {
  _resetSessionOwnerFollowForTests()
  $connectionsRegistry.set(null)
  setConnection(null)
  setSessions([])
  $sessionTiles.set([])
  $profiles.set([])
  clearAllSessionStates()
  _resetSessionOwnerHintsForTests({ storage: true })
  vi.mocked(getSession).mockReset()
  gatewayMocks.requestGatewayForAgent.mockReset()
  gatewayMocks.requestGatewayForProfile.mockReset()
})

describe('(b) session-scoped RPCs go only to the owning gateway', () => {
  it('slash.exec / process.list for a MacBook session with a stale local hint never touch local', async () => {
    // The unified list knows the MacBook owns the id; an older build left a
    // `local` owner hint behind (fabricated-row era), which outranks the row.
    setSessions([makeSessionInfo({ connection_id: MACBOOK, id: MACBOOK_SESSION, profile: 'default' })])
    setSessionOwnerHint(MACBOOK_SESSION, { connectionId: 'local', profile: 'default' })

    const gateways = mockGateways(handlers())
    const request = dispatcher(gateways.ambient, [[MACBOOK_SESSION, 'rt-mb']])

    await request('slash.exec', { command: '/goal status', session_id: 'rt-mb' })
    await request('process.list', { session_id: 'rt-mb' })
    await request('session.control.read', { session_id: 'rt-mb' })

    expect(gateways.on('local')).toHaveLength(0)
    expect(gateways.on(MACBOOK).map(call => call.method)).toEqual([
      'slash.exec',
      'process.list',
      'session.control.read'
    ])
  })

  it('the store poller path (requestForOwnedSession) ignores a stale local tile route too', async () => {
    setSessions([makeSessionInfo({ connection_id: MACBOOK, id: MACBOOK_SESSION, profile: 'default' })])
    $sessionTiles.set([
      {
        ownerRoute: { connectionId: 'local', profile: 'default' },
        runtimeId: 'rt-mb',
        storedSessionId: MACBOOK_SESSION
      }
    ] as never)

    const gateways = mockGateways(handlers())

    await requestForOwnedSession('rt-mb', gateways.ambient as never, 'process.list', { session_id: 'rt-mb' })

    expect(gateways.on('local')).toHaveLength(0)
    expect(gateways.on(MACBOOK)).toHaveLength(1)
  })
})

describe('(c) a not-found from the wrong gateway follows the owner once', () => {
  it('4007: resolves the owner by probe, retries there once, never repeats on the wrong gateway', async () => {
    // No tagged row (the MacBook's pooled backend is still dialing) and a stale
    // local hint: only a by-id probe can find the owner.
    setSessionOwnerHint(MACBOOK_SESSION, { connectionId: 'local', profile: 'default' })
    vi.mocked(getSession).mockImplementation(async (id, scope) => {
      if (id === MACBOOK_SESSION && typeof scope === 'object' && scope?.connectionId === MACBOOK) {
        return makeSessionInfo({ id: MACBOOK_SESSION, profile: 'default' })
      }

      throw new Error('404: Session not found')
    })

    const gateways = mockGateways(handlers())
    const request = dispatcher(gateways.ambient)

    await expect(request('session.activate', { session_id: MACBOOK_SESSION })).resolves.toMatchObject({ ok: true })
    await request('session.control.read', { session_id: MACBOOK_SESSION })
    await request('slash.exec', { command: '/goal status', session_id: MACBOOK_SESSION })

    expect(gateways.on('local')).toHaveLength(1)
    expect(gateways.on(MACBOOK).map(call => call.method)).toEqual([
      'session.activate',
      'session.control.read',
      'slash.exec'
    ])
    expect(getSessionOwnerHint(MACBOOK_SESSION)).toMatchObject({ connectionId: MACBOOK })
  })

  it('4001 on a stale tile route: follows the remembered owner, repoints the tile, stops polling local', async () => {
    // The tile was saved with `local`; the MacBook hint is the only other
    // record. A runtime-id RPC answers 4001 ("not in memory") on local.
    $sessionTiles.set([
      {
        ownerRoute: { connectionId: 'local', profile: 'default' },
        runtimeId: 'rt-mb',
        storedSessionId: MACBOOK_SESSION
      }
    ] as never)
    setSessionOwnerHint(MACBOOK_SESSION, { connectionId: MACBOOK, profile: 'default' })

    const gateways = mockGateways(handlers())
    const request = dispatcher(gateways.ambient)

    for (let tick = 0; tick < 3; tick += 1) {
      await request('process.list', { session_id: 'rt-mb' })
    }

    expect(gateways.on('local')).toHaveLength(1)
    expect(gateways.on(MACBOOK)).toHaveLength(3)
    expect(sessionTileOwnerRoute(MACBOOK_SESSION)).toMatchObject({ connectionId: MACBOOK })
  })

  it('no owner anywhere: one failed request per call, the probe runs once, no retry storm', async () => {
    setSessionOwnerHint(MACBOOK_SESSION, { connectionId: 'local', profile: 'default' })

    const gateways = mockGateways({ local: gateway({}), [MACBOOK]: gateway({}) })
    const request = dispatcher(gateways.ambient)

    for (let tick = 0; tick < 3; tick += 1) {
      await expect(request('session.control.read', { session_id: MACBOOK_SESSION })).rejects.toMatchObject({
        code: 4007
      })
    }

    expect(gateways.on('local')).toHaveLength(3)
    expect(gateways.on(MACBOOK)).toHaveLength(0)
    expect(vi.mocked(getSession)).toHaveBeenCalledTimes(1)
  })
})

describe('(e) control: local sessions stay local', () => {
  it('a local session routes only to local and never probes another gateway', async () => {
    setSessions([makeSessionInfo({ id: LOCAL_SESSION, profile: 'default' })])
    setSessionOwnerHint(LOCAL_SESSION, { connectionId: 'local', profile: 'default' })

    const gateways = mockGateways(handlers())
    const request = dispatcher(gateways.ambient, [[LOCAL_SESSION, 'rt-local']])

    await request('process.list', { session_id: 'rt-local' })
    await request('slash.exec', { command: '/goal status', session_id: 'rt-local' })

    expect(gateways.on(MACBOOK)).toHaveLength(0)
    expect(gateways.on('local')).toHaveLength(2)
    expect(vi.mocked(getSession)).not.toHaveBeenCalled()
  })

  it('a genuinely reaped local runtime (4001) surfaces as before, with no request to another gateway', async () => {
    setSessions([makeSessionInfo({ id: LOCAL_SESSION, profile: 'default' })])
    setSessionOwnerHint(LOCAL_SESSION, { connectionId: 'local', profile: 'default' })

    const gateways = mockGateways(handlers())
    const request = dispatcher(gateways.ambient, [[LOCAL_SESSION, 'rt-dead']])

    await expect(request('process.list', { session_id: 'rt-dead' })).rejects.toMatchObject({ code: 4001 })

    expect(gateways.on(MACBOOK)).toHaveLength(0)
    expect(vi.mocked(getSession)).not.toHaveBeenCalled()
  })
})
