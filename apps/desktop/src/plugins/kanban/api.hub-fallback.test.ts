import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] A gateway whose live board read fails still shows its cards from
// the window gateway's hermes-hub replica, marked `from_hub`; live reads stay
// the primary source, and a comment on a replica card becomes a hub request.

const routed = vi.hoisted(() => ({ id: null as null | string }))
const hub = vi.hoisted(() => ({
  calls: [] as Array<{ path: string; opts: Record<string, unknown> }>,
  tasks: [] as unknown[]
}))

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGatewayConnectionId: () => routed.id
}))
vi.mock('@/api/plugins', () => ({
  pluginRest: vi.fn(async (plugin: string, path: string, opts: Record<string, unknown>) => {
    if (plugin !== 'hub-sync') {
      throw new Error('unexpected plugin')
    }

    hub.calls.push({ opts, path })

    if (path.startsWith('/tasks?')) {
      return { available: true, gateway: 'laptop', tasks: hub.tasks }
    }

    if (path === '/request') {
      return { id: 'hr-1', queued_locally: true, state: 'local' }
    }

    throw new Error('404')
  })
}))

const api = await import('./api')
const { $connectionsRegistry } = await import('@/store/connection-registry-state')
const { queryClient } = await import('@/lib/query-client')
const { _resetHubSyncForTests, rememberHubGatewayName } = await import('@/store/hub-sync')
const { addComment, bindApi, fetchBoard, hubBoardFromTasks, resetGatewayCooldowns } = api

type Call = { opts?: { connectionId?: string; method?: string; body?: unknown }; path: string }

const board = (columns: Record<string, Array<Record<string, unknown>>>) => ({
  assignees: [],
  columns: Object.entries(columns).map(([name, tasks]) => ({ name, tasks })),
  latest_event_id: 0,
  now: 0,
  tenants: []
})

const hubTask = (id: string, extra: Record<string, unknown> = {}) => ({
  board: 'default',
  gateway: 'mini',
  id,
  key: `default/${id}`,
  local: false,
  stale: false,
  status: 'running',
  synced_at: 1000,
  title: id,
  version: 3,
  ...extra
})

const storage = { get: <T>(_key: string, fallback: T) => fallback, remove: vi.fn(), set: vi.fn() }
let calls: Call[] = []
let dispose: () => void = () => undefined

async function bind(respond: (call: Call) => unknown) {
  const socket = vi.fn(() => vi.fn())
  dispose = bindApi(
    (async (path: string, opts?: Call['opts']) => {
      calls.push({ opts, path })
      const out = respond({ opts, path })

      if (out instanceof Error) {
        throw out
      }

      return out
    }) as never,
    storage,
    socket
  )
  await vi.waitFor(() => expect(socket).toHaveBeenCalled())
  calls = []
}

beforeEach(() => {
  routed.id = 'local'
  hub.calls = []
  hub.tasks = []
  _resetHubSyncForTests()
  $connectionsRegistry.set({
    connections: ['local', 'conn-mini'].map(id => ({ id, kind: id === 'local' ? 'local' : 'ssh', label: id })),
    primary: 'local'
  } as never)
})

afterEach(() => {
  dispose()
  $connectionsRegistry.set(null)
  resetGatewayCooldowns()
  queryClient.clear()
})

const liveOrDown = (miniUp: boolean) => (call: Call) => {
  if (call.path.startsWith('/board')) {
    if (call.opts?.connectionId === 'conn-mini') {
      return miniUp ? board({ ready: [{ id: 'live-mini', status: 'ready', title: 'live' }] }) : new Error('timed out')
    }

    return board({ ready: [{ id: 'own', status: 'ready', title: 'own' }] })
  }

  if (call.path.startsWith('/boards')) {
    return { boards: [{ slug: 'default' }], current: 'default' }
  }

  return {}
}

describe('Kanban hub replica fallback', () => {
  it('fills a gateway whose live read failed from the replica, marked from hub', async () => {
    rememberHubGatewayName('conn-mini', 'mini')
    hub.tasks = [hubTask('t_run'), hubTask('t_blk', { status: 'blocked', synced_at: 900 })]
    await bind(liveOrDown(false))

    const result = await fetchBoard(false)
    const all = result.columns.flatMap(col => col.tasks)

    expect(all.map(t => [t.id, t.gateway, Boolean(t.from_hub)])).toEqual([
      ['own', 'local', false],
      ['t_run', 'conn-mini', true],
      ['t_blk', 'conn-mini', true]
    ])
    expect(result.hub_gateways).toEqual({ 'conn-mini': 900 })
    expect(result.gateway_errors).toEqual({ 'conn-mini': 'timed out' }) // still flagged as unreachable
    const read = hub.calls.find(call => call.path.startsWith('/tasks?'))!
    expect(read.opts.connectionId).toBe('local') // the replica lives on the window's own gateway
    expect(new URLSearchParams(read.path.split('?')[1]).get('gateway')).toBe('mini')
  })

  it('keeps the live read as the source when the gateway answers (no replica read)', async () => {
    rememberHubGatewayName('conn-mini', 'mini')
    hub.tasks = [hubTask('stale-copy')]
    await bind(liveOrDown(true))

    const result = await fetchBoard(false)

    expect(result.columns.flatMap(col => col.tasks).map(t => t.id)).toEqual(['own', 'live-mini'])
    expect(result.hub_gateways).toBeUndefined()
    expect(hub.calls.some(call => call.path.startsWith('/tasks?'))).toBe(false)
  })

  it('a gateway with no known hub name stays missing (no guessing)', async () => {
    hub.tasks = [hubTask('t')]
    await bind(liveOrDown(false))

    const result = await fetchBoard(false)

    expect(result.columns.flatMap(col => col.tasks).map(t => t.id)).toEqual(['own'])
    expect(hub.calls.some(call => call.path.startsWith('/tasks?'))).toBe(false)
  })

  it('a comment on a replica card is queued as a hub request; live cards use the kanban API', async () => {
    rememberHubGatewayName('conn-mini', 'mini')
    hub.tasks = [hubTask('t_run')]
    await bind(liveOrDown(false))
    await fetchBoard(false)

    await addComment('t_run', 'ping')
    expect(hub.calls.at(-1)).toMatchObject({
      opts: {
        body: { action: 'comment', args: { text: 'ping' }, coll: 'tasks', gateway: 'mini', key: 'default/t_run' }
      },
      path: '/request'
    })

    calls = []
    await addComment('own', 'hi')
    expect(calls.map(c => c.path)).toEqual(['/tasks/own/comments'])
  })
})

describe('hubBoardFromTasks', () => {
  it('orders lanes like the board and maps run/summary fields', () => {
    const b = hubBoardFromTasks(
      [
        hubTask('d', { status: 'done' }),
        hubTask('r', { assignee: 'w', run: { started_at: 50, status: 'running', summary: 'going' } }),
        hubTask('t', { status: 'triage' })
      ] as never,
      100
    )

    expect(b.columns.map(c => c.name)).toEqual(['triage', 'running', 'done'])
    expect(b.columns[1].tasks[0]).toMatchObject({ current_run_started_at: 50, from_hub: true, latest_summary: 'going' })
    expect(b.assignees).toEqual(['w'])
  })
})
