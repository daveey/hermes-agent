import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] What the renderer reports as use of a remote profile (store/remote-use → hermes:remote:note-use).
// After the hub's default backend reconnected, every pooled hub-profile socket redialed in the same millisecond
// (reconnectSecondaryGateways / the per-entry reconnect ladder → hermes:connection:for + ws-url-for per profile),
// and Desktop main counted each of those dials as use: every listed profile got stamped into the cap-4 prewarm
// set and a 30-min keep-alive hold. Dials and background reads must never report use; user actions must.

const gatewayMocks = vi.hoisted(() => ({
  instances: [] as Array<{ connectionState: string }>,
  connect: vi.fn(async (_wsUrl: string): Promise<void> => undefined),
  setConnection: vi.fn(),
  setGatewayState: vi.fn()
}))

vi.mock('@/hermes', () => ({
  setApiRequestConnection: vi.fn(),
  HermesGateway: class {
    connectionState = 'closed'
    constructor() {
      gatewayMocks.instances.push(this)
    }
    connect = async (wsUrl: string): Promise<void> => {
      await gatewayMocks.connect(wsUrl)
      this.connectionState = 'open'
    }
    close = (): void => {
      this.connectionState = 'closed'
    }
    request = async (): Promise<unknown> => ({})
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
  }
}))
vi.mock('@/store/session', () => ({
  setConnection: gatewayMocks.setConnection,
  setGatewayState: gatewayMocks.setGatewayState
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const {
  closeSecondaryGateways,
  configureGatewayRegistry,
  openGatewayForAgent,
  reconnectSecondaryGateways,
  requestGatewayForAgent,
  retainGatewayForAgent,
  retainGatewayForSessionTurn,
  setPrimaryGateway,
  setPrimaryGatewayConnectionId
} = await import('./gateway')

const { _resetRemoteUseForTests } = await import('./remote-use')

const HUB = 'hermes-hub'
const HUB_PROFILES = ['bb-tuner', 'coach', 'research', 'alpha']

const hubConn = {
  authMode: 'token',
  baseUrl: 'http://127.0.0.1:50001',
  mode: 'remote',
  profile: 'default',
  token: 'fake-test-token',
  wsUrl: 'ws://127.0.0.1:50001/api/ws?token=fake-test-token'
}

let desktop: {
  getConnection: ReturnType<typeof vi.fn>
  getConnectionFor: ReturnType<typeof vi.fn>
  getGatewayWsUrlFor: ReturnType<typeof vi.fn>
  noteRemoteUse: ReturnType<typeof vi.fn>
}

beforeEach(() => {
  configureGatewayRegistry({ onEvent: vi.fn() })
  setPrimaryGateway({ connectionState: 'open' } as never, 'default')
  desktop = {
    getConnection: vi.fn(async () => hubConn),
    getConnectionFor: vi.fn(async () => hubConn),
    getGatewayWsUrlFor: vi.fn(async () => hubConn.wsUrl),
    noteRemoteUse: vi.fn(async () => true)
  }
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = desktop
  gatewayMocks.instances.length = 0
  _resetRemoteUseForTests()
})

afterEach(() => {
  closeSecondaryGateways()
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

const flush = async () => {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve()
  }
}

describe('remote profile use reporting', () => {
  it('a post-reconnect sweep of N pooled hub profiles redials each one but reports no use', async () => {
    // Pooled hub-profile sockets held by background surfaces (roster, sidebar, relay).
    const releases: Array<() => void> = []

    for (const profile of HUB_PROFILES) {
      releases.push(await retainGatewayForAgent(HUB, profile))
    }

    expect(desktop.getConnectionFor).toHaveBeenCalledTimes(HUB_PROFILES.length)

    // The hub tunnel drops: every pooled socket is closed. The primary's reconnect then sweeps them all.
    for (const gateway of gatewayMocks.instances) {
      gateway.connectionState = 'closed'
    }

    const dialed = () =>
      [...desktop.getConnectionFor.mock.calls, ...desktop.getGatewayWsUrlFor.mock.calls].map(
        ([payload]) => (payload as { profile: string }).profile
      )

    desktop.getConnectionFor.mockClear()
    desktop.getGatewayWsUrlFor.mockClear()
    reconnectSecondaryGateways()
    await flush()
    await vi.waitFor(() => expect(new Set(dialed())).toEqual(new Set(HUB_PROFILES)), { timeout: 15_000 })

    // Background reads on the reopened sockets (session lists, roster) do not count either.
    for (const profile of HUB_PROFILES) {
      await requestGatewayForAgent(HUB, profile, 'session.list', {})
    }

    releases.forEach(release => release())
    expect(desktop.noteRemoteUse).not.toHaveBeenCalled()
  })

  it('a real dispatch reports use: foreground open / create, and a routed prompt.submit turn', async () => {
    await openGatewayForAgent(HUB, 'bb-tuner', { spawnPriority: 'foreground' })
    expect(desktop.noteRemoteUse).toHaveBeenLastCalledWith({ connectionId: HUB, profile: 'bb-tuner' })

    await requestGatewayForAgent(HUB, 'coach', 'session.create', {}, undefined, undefined, {
      spawnPriority: 'foreground'
    })
    expect(desktop.noteRemoteUse).toHaveBeenLastCalledWith({ connectionId: HUB, profile: 'coach' })

    const release = await retainGatewayForAgent(HUB, 'alpha', { spawnPriority: 'foreground' })
    release()
    expect(desktop.noteRemoteUse).toHaveBeenLastCalledWith({ connectionId: HUB, profile: 'alpha' })

    const releaseTurn = await retainGatewayForSessionTurn(HUB, 'research', 'session-1')
    releaseTurn()
    expect(desktop.noteRemoteUse).toHaveBeenLastCalledWith({ connectionId: HUB, profile: 'research' })

    expect(desktop.noteRemoteUse).toHaveBeenCalledTimes(4)
  })

  it('repeat sends on one route report at most once a minute; a bare profile route never reports', async () => {
    await retainGatewayForSessionTurn(HUB, 'research', 'session-1').then(release => release())
    await retainGatewayForSessionTurn(HUB, 'research', 'session-2').then(release => release())
    await openGatewayForAgent(null, 'research', { spawnPriority: 'foreground' })

    expect(desktop.noteRemoteUse).toHaveBeenCalledTimes(1)
  })

  it("a send on the window's own (remote primary) gateway is a use of the primary's route", async () => {
    setPrimaryGateway({ connectionState: 'open' } as never, 'research')
    setPrimaryGatewayConnectionId(HUB, 'remote')

    await retainGatewayForSessionTurn(null, 'research', 'session-1').then(release => release())

    expect(desktop.noteRemoteUse).toHaveBeenCalledExactlyOnceWith({ connectionId: HUB, profile: 'research' })
  })
})
