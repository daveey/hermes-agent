import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] fetchBlockedTasks — the left pane's Blocked section: every blocked
// card on every board of every gateway.

const routed = vi.hoisted(() => ({ id: null as null | string }))

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGatewayConnectionId: () => routed.id
}))

const api = await import('./api')
const { $connectionsRegistry } = await import('@/store/connection-registry-state')
const { queryClient } = await import('@/lib/query-client')

const { $boardSlug, bindApi, fetchBlockedTasks, resetGatewayCooldowns, taskGateway } = api

type Call = { opts?: { connectionId?: string; method?: string; body?: unknown }; path: string }

const task = (id: string, extra: Record<string, unknown> = {}) => ({ id, status: 'ready', title: id, ...extra })

const board = (columns: Record<string, ReturnType<typeof task>[]>, extra: Record<string, unknown> = {}) => ({
  assignees: [],
  columns: Object.entries(columns).map(([name, tasks]) => ({ name, tasks })),
  latest_event_id: 0,
  now: 0,
  tenants: [],
  ...extra
})

const storage = { get: <T>(_key: string, fallback: T) => fallback, remove: vi.fn(), set: vi.fn() }

function registry(...ids: string[]) {
  $connectionsRegistry.set({
    connections: ids.map(id => ({ id, kind: id === 'local' ? 'local' : 'ssh', label: id.toUpperCase() })),
    primary: 'local'
  } as never)
}

let calls: Call[] = []
let dispose: () => void = () => undefined

async function bind(respond: (call: Call) => unknown) {
  const socket = vi.fn(() => vi.fn())
  calls = []
  dispose = bindApi(
    (async (path: string, opts?: Call['opts']) => {
      const call = { opts, path }
      calls.push(call)
      const out = respond(call)

      if (out instanceof Error) {
        throw out
      }

      return out
    }) as never,
    storage,
    socket
  )
  // bindApi's own events-socket snapshot read is not what these tests assert.
  await vi.waitFor(() => expect(socket).toHaveBeenCalled())
  calls = []
}

beforeEach(() => {
  routed.id = 'local'
})

afterEach(() => {
  dispose()
  $connectionsRegistry.set(null)
  $boardSlug.set('')
  resetGatewayCooldowns()
  queryClient.clear()
  vi.useRealTimers()
})

describe('fetchBlockedTasks', () => {
  it('reads only boards whose counts hold blocked cards and tags each card with its board', async () => {
    registry('local')
    await bind(call => {
      if (call.path === '/boards') {
        return {
          boards: [
            { counts: { blocked: 1, ready: 3 }, slug: 'default' },
            { counts: { ready: 2 }, slug: 'idle' },
            { counts: { blocked: 1 }, slug: 'ops' }
          ],
          current: 'default'
        }
      }

      if (call.path === '/board?board=default') {
        return board({ blocked: [task('b1', { created_at: 5, status: 'blocked' })], ready: [task('r1')] })
      }

      if (call.path === '/board?board=ops') {
        return board({ blocked: [task('b2', { created_at: 9, priority: 1, status: 'blocked' })] })
      }

      return new Error(`unexpected ${call.path}`)
    })

    const tasks = await fetchBlockedTasks()

    expect(calls.map(c => c.path)).toEqual(['/boards', '/board?board=default', '/board?board=ops'])
    // Priority first, then newest.
    expect(tasks.map(t => [t.id, t.board])).toEqual([
      ['b2', 'ops'],
      ['b1', 'default']
    ])
  })

  it('fans out to every gateway, survives a dead one, and routes the drawer to the owner', async () => {
    registry('local', 'mini', 'cloud')
    await bind(call => {
      const gw = call.opts?.connectionId ?? 'local'

      if (gw === 'cloud') {
        return new Error('timed out')
      }

      if (call.path === '/boards') {
        return { boards: [{ counts: { blocked: 1 }, slug: 'default' }], current: 'default' }
      }

      return board({ blocked: [task(`${gw}-b`, { status: 'blocked' })] })
    })

    const tasks = await fetchBlockedTasks()

    expect(tasks.map(t => [t.id, t.gateway]).sort()).toEqual([
      ['local-b', 'local'],
      ['mini-b', 'mini']
    ])
    expect(taskGateway('mini-b')).toBe('mini')
  })

  it("resolves each managing session to its live tip and title on the card's gateway", async () => {
    registry('local')
    await bind(call => {
      if (call.path === '/boards') {
        return { boards: [{ counts: { blocked: 3 }, slug: 'default' }], current: 'default' }
      }

      if (call.path === '/board?board=default') {
        return board({
          blocked: [
            task('a', { managing_session_id: 'old-id', session_id: 'creator', status: 'blocked' }),
            task('b', { managing_session_id: null, session_id: 'creator', status: 'blocked' }),
            task('c', { status: 'blocked' })
          ]
        })
      }

      if (call.path.startsWith('/session-labels?')) {
        return {
          profile: 'bb',
          sessions: { creator: { tip_id: 'creator', title: '' }, 'old-id': { tip_id: 'new-id', title: 'Ship it' } }
        }
      }

      return new Error(`unexpected ${call.path}`)
    })

    const tasks = await fetchBlockedTasks()

    expect(calls.map(c => c.path)).toContain(`/session-labels?ids=${encodeURIComponent('old-id,creator')}`)
    expect(Object.fromEntries(tasks.map(t => [t.id, t.manager]))).toEqual({
      a: { id: 'new-id', profile: 'bb', title: 'Ship it' },
      b: { id: 'creator', profile: 'bb', title: null },
      c: null
    })
  })

  it('treats omitted manager ids as creator-owned cards, retaining the creator', async () => {
    registry('local')
    await bind(call => {
      if (call.path === '/boards') {
        return { boards: [{ counts: { blocked: 2 }, slug: 'bb' }], current: 'bb' }
      }

      if (call.path === '/board?board=bb') {
        return board({
          blocked: ['internal-a', 'internal-b'].map(session_id =>
            task(session_id, { created_by: 'bb-dev', session_id, status: 'blocked' })
          )
        })
      }

      if (call.path.startsWith('/session-labels?')) {
        return { profile: 'default', sessions: {} }
      }

      return new Error(`unexpected ${call.path}`)
    })

    const tasks = await fetchBlockedTasks()
    expect(tasks.map(t => [t.manager, t.creator])).toEqual([
      [null, 'bb-dev'],
      [null, 'bb-dev']
    ])
  })

  it('an older backend without /session-labels still groups by the raw id', async () => {
    registry('local')
    await bind(call => {
      if (call.path === '/boards') {
        return { boards: [{ counts: { blocked: 1 }, slug: 'default' }], current: 'default' }
      }

      if (call.path === '/board?board=default') {
        return board({ blocked: [task('a', { session_id: 'creator', status: 'blocked' })] })
      }

      return new Error('404: Not Found')
    })

    const [only] = await fetchBlockedTasks()

    expect(only.manager).toEqual({ id: 'creator', profile: null, title: null })
  })

  it('throws when no gateway answers', async () => {
    registry('local')
    await bind(() => new Error('down'))

    await expect(fetchBlockedTasks()).rejects.toThrow('down')
  })
})
