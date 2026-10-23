import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] findForeignSessionOwner consults the hermes-hub replica (one local
// read on the primary gateway) BEFORE dialing every connection with a by-id
// probe; the probe stays the fallback when the replica misses.

const hub = vi.hoisted(() => ({ hits: [] as unknown[], calls: 0 }))

vi.mock('@/api/plugins', () => ({
  pluginRest: vi.fn(async (plugin: string, path: string) => {
    if (plugin === 'hub-sync' && path.startsWith('/where/')) {
      hub.calls += 1

      return { available: true, gateway: 'laptop', hits: hub.hits }
    }

    throw new Error('404')
  })
}))

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<Record<string, unknown>>()),
  getSession: vi.fn()
}))

const { getSession } = await import('@/hermes')
const { $connectionsRegistry } = await import('@/store/connection-registry-state')
const { _resetHubSyncForTests, rememberHubGatewayName } = await import('@/store/hub-sync')
const { _resetSessionOwnerHintsForTests, setSessions } = await import('@/store/session')
const { findForeignSessionOwner, _resetSessionOwnerFollowForTests } = await import('./owner-follow')

const SID = '20261023_120000_abcdef'

function registry(...ids: string[]) {
  $connectionsRegistry.set({
    connections: ids.map(id => ({ id, kind: id === 'local' ? 'local' : 'ssh', label: id })),
    primary: 'local'
  } as never)
}

beforeEach(() => {
  _resetHubSyncForTests()
  _resetSessionOwnerFollowForTests()
  _resetSessionOwnerHintsForTests({ storage: true })
  setSessions([])
  hub.hits = []
  hub.calls = 0
  vi.mocked(getSession).mockReset()
})

afterEach(() => {
  $connectionsRegistry.set(null)
})

describe('findForeignSessionOwner + hub replica', () => {
  it('answers from the replica without probing any connection', async () => {
    registry('local', 'conn-mini', 'cloud')
    rememberHubGatewayName('conn-mini', 'mini')
    hub.hits = [{ gateway: 'mini', local: false, profile: 'bb', session: SID }]

    expect(await findForeignSessionOwner(SID, ['local'])).toEqual({ connectionId: 'conn-mini', profile: 'bb' })
    expect(getSession).not.toHaveBeenCalled()
  })

  it('works for an owner that is asleep: no probe, so no wait on a dead connection', async () => {
    registry('local', 'conn-mini')
    rememberHubGatewayName('conn-mini', 'mini')
    vi.mocked(getSession).mockImplementation(() => new Promise(() => undefined)) // a hung gateway
    hub.hits = [{ gateway: 'mini', profile: 'default' }]

    expect(await findForeignSessionOwner(SID, ['local'])).toEqual({ connectionId: 'conn-mini', profile: 'default' })
  })

  it('falls back to the by-id probe when the replica does not know the id', async () => {
    registry('local', 'cloud')
    vi.mocked(getSession).mockResolvedValue({ id: SID, profile: 'alpha' } as never)

    expect(await findForeignSessionOwner(SID, ['local'])).toEqual({ connectionId: 'cloud', profile: 'alpha' })
    expect(hub.calls).toBe(1)
    expect(getSession).toHaveBeenCalledTimes(1)
  })

  it('never names an excluded connection (the 4007 sender), and probe:false still asks only the replica', async () => {
    registry('local', 'conn-mini')
    rememberHubGatewayName('conn-mini', 'mini')
    hub.hits = [{ gateway: 'mini', profile: 'default' }]

    expect(await findForeignSessionOwner(SID, ['conn-mini'], { probe: false })).toBeUndefined()
    expect(getSession).not.toHaveBeenCalled()
  })

  it('is inert on a single-gateway Desktop', async () => {
    registry('local')
    hub.hits = [{ gateway: 'laptop', local: true, profile: 'default' }]

    expect(await findForeignSessionOwner(SID, ['local'])).toBeUndefined()
    expect(hub.calls).toBe(0)
  })
})
