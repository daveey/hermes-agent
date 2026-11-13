import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] hermes-hub replica door: gateway-name mapping, session owner lookup,
// requests. pluginRest is mocked per (path, connection) so a call that goes to
// the wrong gateway is visible.

const rest = vi.hoisted(() => ({ handler: null as null | ((path: string, opts: Record<string, unknown>) => unknown) }))

vi.mock('@/api/plugins', () => ({
  pluginRest: vi.fn(async (plugin: string, path: string, opts: Record<string, unknown>) => {
    if (plugin !== 'hub-sync' || !rest.handler) {
      throw new Error('unexpected')
    }

    const out = rest.handler(path, opts)

    if (out instanceof Error) {
      throw out
    }

    return out
  })
}))

const { pluginRest } = await import('@/api/plugins')
const { $connectionsRegistry } = await import('@/store/connection-registry-state')

const {
  _resetHubSyncForTests,
  connectionForHubGateway,
  HUB_GATEWAY_NAMES_KEY,
  hubGatewayForConnection,
  hubRequest,
  hubSessionOwner,
  learnHubGatewayName,
  rememberHubGatewayName
} = await import('./hub-sync')

function registry(...connections: Array<{ id: string; label?: string; host?: string }>) {
  $connectionsRegistry.set({
    connections: connections.map(c => ({ kind: c.id === 'local' ? 'local' : 'ssh', label: c.id, ...c })),
    primary: 'local'
  } as never)
}

beforeEach(() => {
  _resetHubSyncForTests()
  vi.mocked(pluginRest).mockClear()
  rest.handler = null
})

afterEach(() => {
  $connectionsRegistry.set(null)
})

describe('gateway names', () => {
  it('uses passive reads before registry hydration and for non-primary probes', async () => {
    $connectionsRegistry.set(null)
    rest.handler = () => new Error('backend not running')
    expect(await learnHubGatewayName('local')).toBe(false)
    expect(pluginRest).toHaveBeenLastCalledWith(
      'hub-sync',
      '/status',
      expect.objectContaining({ connectionId: 'local', passive: true })
    )
    registry({ id: 'local' }, { id: 'remote' })
    rest.handler = () => ({ gateway: 'remote', available: true })
    expect(await learnHubGatewayName('remote')).toBe(true)
    expect(pluginRest).toHaveBeenLastCalledWith(
      'hub-sync',
      '/status',
      expect.objectContaining({ connectionId: 'remote', passive: true })
    )
  })

  it("learns a connection's hub name from its own /status and persists it", async () => {
    registry({ id: 'local' }, { id: 'conn-7', label: 'Studio' })
    rest.handler = (path, opts) =>
      path === '/status' && opts.connectionId === 'conn-7' ? { available: true, gateway: 'mini' } : new Error('x')

    expect(await learnHubGatewayName('conn-7')).toBe(true)
    expect(hubGatewayForConnection('conn-7')).toBe('mini')
    expect(connectionForHubGateway('mini')).toBe('conn-7')
    expect(JSON.parse(localStorage.getItem(HUB_GATEWAY_NAMES_KEY) ?? '{}')).toEqual({ 'conn-7': 'mini' })
    // cached: no second /status within the TTL
    await learnHubGatewayName('conn-7')
    expect(pluginRest).toHaveBeenCalledTimes(1)
  })

  it('a gateway without hub-sync (or hermes-hub) maps to nothing', async () => {
    registry({ id: 'local' }, { id: 'cloud' })
    rest.handler = () => ({ available: false, gateway: null })

    expect(await learnHubGatewayName('cloud')).toBe(false)
    expect(hubGatewayForConnection('cloud')).toBeUndefined()
  })

  it('falls back to an id / label / host match, never to a connection that proved another name', () => {
    registry({ id: 'local' }, { id: 'a1', label: 'Mac mini', host: 'mini.example.com' }, { id: 'macbook' })
    expect(connectionForHubGateway('mini')).toBe('a1')
    expect(connectionForHubGateway('macbook')).toBe('macbook')
    expect(connectionForHubGateway('nowhere')).toBeUndefined()
    rememberHubGatewayName('a1', 'studio')
    expect(connectionForHubGateway('mini')).toBeUndefined()
    expect(connectionForHubGateway('studio')).toBe('a1')
  })

  it('a re-added connection takes the hub name over', () => {
    registry({ id: 'local' }, { id: 'new' })
    rememberHubGatewayName('old', 'mini')
    rememberHubGatewayName('new', 'mini')
    expect(connectionForHubGateway('mini')).toBe('new')
    expect(hubGatewayForConnection('old')).toBeUndefined()
  })

  it('a slow /status is single-flight: overlapping askers share one read', async () => {
    registry({ id: 'local' }, { id: 'slow' })
    let answer!: (value: unknown) => void
    rest.handler = () => new Promise(resolve => (answer = resolve))

    const asks = [learnHubGatewayName('slow'), learnHubGatewayName('slow'), learnHubGatewayName('slow')]

    await Promise.resolve()
    expect(pluginRest).toHaveBeenCalledTimes(1)
    answer({ available: true, gateway: 'mini' })
    expect(await Promise.all(asks)).toEqual([true, true, true])
    expect(pluginRest).toHaveBeenCalledTimes(1)
  })

  it('a failed or hub-less /status is not re-asked on every board poll', async () => {
    vi.useFakeTimers()

    try {
      registry({ id: 'local' }, { id: 'cloud' })
      rest.handler = () => ({ available: false, gateway: null })

      expect(await learnHubGatewayName('cloud')).toBe(false)
      vi.advanceTimersByTime(15_000)
      expect(await learnHubGatewayName('cloud')).toBe(false)
      expect(pluginRest).toHaveBeenCalledTimes(1)
      vi.advanceTimersByTime(60_000)
      rest.handler = () => ({ available: true, gateway: 'cloud-gw' })
      expect(await learnHubGatewayName('cloud')).toBe(true)
      expect(pluginRest).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('hubSessionOwner', () => {
  it('maps the replica owner to its registry connection; own records map to the primary', async () => {
    registry({ id: 'local' }, { id: 'conn-mini', label: 'mini' })

    rest.handler = (path, opts) => {
      expect(opts.connectionId).toBe('local') // reads always go to the window's own gateway

      if (path === '/where/s_remote') {
        return { available: true, gateway: 'macbook', hits: [{ gateway: 'mini', local: false, profile: 'bb' }] }
      }

      if (path === '/where/s_own') {
        return { available: true, gateway: 'macbook', hits: [{ gateway: 'macbook', local: true, profile: 'default' }] }
      }

      return { available: true, gateway: 'macbook', hits: [] }
    }

    expect(await hubSessionOwner('s_remote')).toEqual({ connectionId: 'conn-mini', profile: 'bb' })
    expect(await hubSessionOwner('s_own')).toEqual({ connectionId: 'local', profile: 'default' })
    expect(await hubSessionOwner('s_unknown')).toBeUndefined()
    expect(hubGatewayForConnection('local')).toBe('macbook') // every answer names its gateway
  })

  it('skips excluded connections, prefers live rows over archived copies, caches lookups', async () => {
    registry({ id: 'local' }, { id: 'mini' }, { id: 'cloud' })
    rest.handler = () => ({
      available: true,
      gateway: 'laptop',
      hits: [
        { archived: true, gateway: 'cloud', profile: 'old' },
        { gateway: 'mini', profile: 'default' }
      ]
    })

    expect(await hubSessionOwner('s')).toEqual({ connectionId: 'mini', profile: 'default' })
    expect(await hubSessionOwner('s', ['mini'])).toEqual({ connectionId: 'cloud', profile: 'old' })
    expect(pluginRest).toHaveBeenCalledTimes(1)
  })

  it('an unregistered owner, a missing plugin or an error yields undefined', async () => {
    registry({ id: 'local' }, { id: 'mini' })
    rest.handler = () => ({ available: true, gateway: 'laptop', hits: [{ gateway: 'elsewhere', profile: 'x' }] })
    expect(await hubSessionOwner('a')).toBeUndefined()
    rest.handler = () => new Error('404 Not Found')
    expect(await hubSessionOwner('b')).toBeUndefined()
  })
})

describe('hubRequest', () => {
  it("posts to the primary gateway's hub-sync", async () => {
    registry({ id: 'local' }, { id: 'mini' })
    rest.handler = (path, opts) => ({ id: 'hr-1', path, opts, queued_locally: true, state: 'local' })

    const body = { action: 'archive', coll: 'sessions', gateway: 'mini', key: 'default/s' } as const
    const res = (await hubRequest(body)) as unknown as { path: string; opts: Record<string, unknown> }

    expect(res.path).toBe('/request')
    expect(res.opts).toMatchObject({ body, connectionId: 'local', method: 'POST' })
  })
})
