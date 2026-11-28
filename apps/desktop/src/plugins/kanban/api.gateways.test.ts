import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// [daveey] One board across every registered gateway: reads fan out and merge,
// writes route back to the task's owning gateway. See the gateways section of
// ./api.ts.

const routed = vi.hoisted(() => ({ id: null as null | string }))

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGatewayConnectionId: () => routed.id
}))

const api = await import('./api')
const { $connectionsRegistry } = await import('@/store/connection-registry-state')
const { queryClient } = await import('@/lib/query-client')

const {
  $boardSlug,
  bindApi,
  bulkTasks,
  createTask,
  fetchBoard,
  fetchBoards,
  mergeBoardLists,
  mergeBoards,
  patchTask,
  resetGatewayCooldowns
} = api

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

describe('qualified task routes', () => {
  it('routes a drawer foreign_card 409 to its owner without losing profile or board', async () => {
    registry('local', 'hub')
    await bind(({ path, opts }) =>
      path.includes('/comments') && opts?.connectionId !== 'hub'
        ? new Error(
            'HTTP 409: {"detail":"foreign_card: t_drawer was created on hub and lives there; this gateway has only a replica copy. kanban_supervisor_request hub-comment"}'
          )
        : { ok: true }
    )
    calls = []
    await expect(
      api.addComment('t_drawer', 'Already answered, here is more context', {
        gateway: 'local',
        profile: 'research',
        board: 'ops'
      })
    ).resolves.toEqual({ ok: true })
    expect(calls).toEqual([
      {
        path: '/tasks/t_drawer/comments?board=ops',
        opts: {
          connectionId: 'local',
          profile: 'research',
          method: 'POST',
          body: { author: 'desktop', body: 'Already answered, here is more context' }
        }
      },
      {
        path: '/tasks/t_drawer/comments?board=ops',
        opts: {
          connectionId: 'hub',
          profile: 'research',
          method: 'POST',
          body: { author: 'desktop', body: 'Already answered, here is more context' }
        }
      }
    ])
  })

  it('explains an unreachable foreign owner without worker-only instructions', async () => {
    registry('local')
    await bind(({ path }) =>
      path.includes('/comments')
        ? new Error(
            'HTTP 409: foreign_card: t_unreachable was created on worker-8 and lives there; kanban_supervisor_request hub-comment'
          )
        : { ok: true }
    )
    calls = []
    const result = api.addComment('t_unreachable', 'context', {
      gateway: 'local',
      profile: 'default',
      board: 'default'
    })
    await expect(result).rejects.toThrow('Your comment was not saved. Connect to that gateway')
    await expect(result).rejects.not.toThrow(/kanban_supervisor_request|hub-comment/)
    expect(calls).toHaveLength(1)
  })

  it('keeps the delayed dispatcher nudge on the immutable owner profile and board', async () => {
    await bind(() => board({ ready: [] }))
    const owner = { gateway: 'nudge-owner', profile: 'work', board: 'ops' }
    calls = []
    await api.patchTask('nudge-task', { priority: 1 }, owner)
    routed.id = 'elsewhere'
    $boardSlug.set('other')
    await vi.waitFor(() =>
      expect(calls).toContainEqual({
        path: '/dispatch?board=ops',
        opts: { connectionId: 'nudge-owner', profile: 'work', method: 'POST', body: {} }
      })
    )
  })

  it('rejects ambiguous legacy ids rather than using whichever owner was remembered last', async () => {
    await bind(() => board({ ready: [] }))
    api.rememberTaskOwner('ambiguous', 'ops', 'a', 'p')
    api.rememberTaskOwner('ambiguous', 'ops', 'b', 'p')
    calls = []
    await expect(api.fetchTask('ambiguous')).rejects.toThrow(/ambiguous/i)
    expect(calls).toEqual([])
    await api.fetchTask('ambiguous', { gateway: 'a', profile: 'p', board: 'ops' })
    expect(calls[0]).toMatchObject({ opts: { connectionId: 'a', profile: 'p' } })
  })

  it('pins every drawer action, log and delayed attachment download to the supplied owner', async () => {
    const save = vi.fn().mockResolvedValue({ saved: true })
    vi.stubGlobal('hermesDesktop', { saveGatewayFile: save })
    await bind(() => board({ ready: [] }))
    const owner = { gateway: 'drawer-owner', profile: 'work', board: 'private' }
    api.rememberTaskOwner('drawer-id', 'wrong', 'other', 'default')
    calls = []
    await api.fetchLog('drawer-id', owner)
    await api.addComment('drawer-id', 'hello', owner)
    await api.reclaimTask('drawer-id', owner)
    await api.reassignTask('drawer-id', 'worker', owner)
    await api.estimateTask('drawer-id', owner)
    await api.uploadAttachment('drawer-id', { filename: 'a', bytes: new ArrayBuffer(0) }, owner)
    await api.deleteTask('drawer-id', owner)
    const detail = await api.fetchTask('drawer-id', owner)
    routed.id = 'other'
    await detail.downloadAttachment('/stored/a', 'a')

    for (const call of calls) {
      expect(call.opts).toMatchObject({ connectionId: owner.gateway, profile: owner.profile })
      expect(new URL(call.path, 'http://example.com').searchParams.get('board')).toBe(owner.board)
    }

    expect(save).toHaveBeenCalledWith({
      connectionId: owner.gateway,
      profile: owner.profile,
      path: '/stored/a',
      suggestedName: 'a'
    })
    expect(api.logKey('local', '', 'drawer-id', owner)).not.toEqual(
      api.logKey('local', '', 'drawer-id', { ...owner, profile: 'other' })
    )
    vi.unstubAllGlobals()
  })

  it('keeps colliding task reads and writes on the explicit owner after ambient and registry changes', async () => {
    await bind(() => board({ ready: [] }))
    const owner = { gateway: 'owner-a', profile: 'research', board: 'ops' }
    api.rememberTaskOwner('collision', 'other', 'owner-b', 'default')
    routed.id = 'owner-b'
    calls = []
    await api.fetchTask('collision', owner)
    await api.patchTask('collision', { priority: 2 }, owner)
    expect(calls).toEqual([
      { path: '/tasks/collision?board=ops', opts: { connectionId: 'owner-a', profile: 'research' } },
      {
        path: '/tasks/collision?board=ops',
        opts: { connectionId: 'owner-a', profile: 'research', method: 'PATCH', body: { priority: 2 } }
      }
    ])
    expect(api.taskKey('local', '', 'collision', owner)).not.toEqual(
      api.taskKey('local', '', 'collision', { ...owner, profile: 'default' })
    )
  })
})

describe('mergeBoards', () => {
  it('tags every card with its gateway and keeps the dispatch order across gateways', () => {
    const merged = mergeBoards([
      {
        board: board(
          { ready: [task('a', { created_at: 5, priority: 0 })], done: [task('d1', { completed_at: 10 })] },
          { assignees: ['x'], latest_event_id: 42, now: 100 }
        ),
        gateway: 'local'
      },
      {
        board: board(
          {
            ready: [task('b', { created_at: 9, priority: 2 }), task('c', { created_at: 1, priority: 0 })],
            done: [task('d2', { completed_at: 20 })]
          },
          { assignees: ['y', 'x'], latest_event_id: 7 }
        ),
        gateway: 'mini'
      }
    ])

    const ready = merged.columns.find(col => col.name === 'ready')!.tasks
    expect(ready.map(t => [t.id, t.gateway])).toEqual([
      ['b', 'mini'],
      ['c', 'mini'],
      ['a', 'local']
    ])
    expect(merged.columns.find(col => col.name === 'done')!.tasks.map(t => t.id)).toEqual(['d2', 'd1'])
    expect(merged.assignees).toEqual(['x', 'y'])
    // The routed (first) gateway's cursor drives the events socket.
    expect(merged.latest_event_id).toBe(42)
    expect(merged.gateway_errors).toBeUndefined()
  })

  it("shows a shadow board's remote copy once, and never over the owner's own live row", () => {
    const merged = mergeBoards([
      {
        board: board({
          ready: [task('own', { title: 'hub own' }), task('r1', { home_gateway: 'mini', remote: true })]
        }),
        gateway: 'local'
      },
      {
        board: board({
          ready: [task('r1', { title: 'mini live' }), task('r2', { home_gateway: 'mini', remote: true })]
        }),
        gateway: 'mini'
      },
      { board: board({ ready: [task('r2', { home_gateway: 'mini', remote: true })] }), gateway: 'cloud' }
    ])

    const ready = merged.columns.find(col => col.name === 'ready')!.tasks

    expect(ready.map(t => [t.id, t.gateway, Boolean(t.remote)]).sort()).toEqual([
      ['own', 'local', false],
      ['r1', 'mini', false],
      ['r2', 'mini', true]
    ])
    expect(ready.find(t => t.id === 'r1')!.title).toBe('mini live')
  })

  it('skips a gateway without the board silently and reports other failures', () => {
    const merged = mergeBoards([
      { board: board({ ready: [task('a')] }), gateway: 'local' },
      { error: new Error('404: {"detail":"board \'ops\' does not exist"}'), gateway: 'mini' },
      { error: new Error('timed out'), gateway: 'cloud' }
    ])

    expect(merged.columns[0].tasks.map(t => t.id)).toEqual(['a'])
    expect(merged.gateway_errors).toEqual({ cloud: 'timed out' })
  })

  it('throws only when no gateway answered', () => {
    expect(() =>
      mergeBoards([
        { error: new Error('down'), gateway: 'local' },
        { error: new Error('404: nope'), gateway: 'mini' }
      ])
    ).toThrow('down')
  })
})

describe('mergeBoardLists', () => {
  it('unions boards by slug, sums totals and records which gateways carry each', () => {
    const merged = mergeBoardLists([
      { boards: { boards: [{ slug: 'default', total: 2 }], current: 'default' }, gateway: 'local' },
      {
        boards: {
          boards: [
            { slug: 'default', total: 3 },
            { slug: 'ops', total: 1 }
          ],
          current: 'ops'
        },
        gateway: 'mini'
      },
      { boards: null, gateway: 'cloud' }
    ])

    expect(merged.current).toBe('default')
    expect(merged.boards).toEqual([
      { gateways: ['local', 'mini'], is_current: true, slug: 'default', total: 5 },
      { gateways: ['mini'], is_current: false, slug: 'ops', total: 1 }
    ])
  })
})

describe('gateway routing', () => {
  it('keeps a remembered board when its owner is the active gateway', async () => {
    registry('local')
    await bind(() => board({ ready: [] }))
    api.rememberTaskOwner('pinned-board', 'owner-board', 'local')
    calls = []

    await api.fetchTask('pinned-board')
    await patchTask('pinned-board', { status: 'todo' })

    expect(calls.map(call => call.path)).toEqual([
      '/tasks/pinned-board?board=owner-board',
      '/tasks/pinned-board?board=owner-board'
    ])
  })

  it('pins an explicit owner profile for task reads and writes on the active gateway', async () => {
    registry('local')
    await bind(() => board({ ready: [] }))
    api.rememberTaskOwner('pinned-profile', 'ops', 'local', 'worker')
    calls = []

    await api.fetchTask('pinned-profile')
    await patchTask('pinned-profile', { status: 'todo' })

    expect(calls).toEqual([
      { path: '/tasks/pinned-profile?board=ops', opts: { connectionId: 'local', profile: 'worker' } },
      {
        path: '/tasks/pinned-profile?board=ops',
        opts: { connectionId: 'local', profile: 'worker', method: 'PATCH', body: { status: 'todo' } }
      }
    ])
  })

  it('partitions bulk writes by owner board and profile even on the active gateway', async () => {
    registry('local')
    await bind(call =>
      call.path.startsWith('/tasks/bulk')
        ? { results: (call.opts?.body as { ids: string[] }).ids.map(id => ({ id, ok: true })) }
        : board({ ready: [] })
    )
    api.rememberTaskOwner('bulk-a', 'ops', 'local', 'worker-a')
    api.rememberTaskOwner('bulk-b', 'ops', 'local', 'worker-b')
    api.rememberTaskOwner('bulk-c', 'other', 'local', 'worker-a')
    calls = []
    await bulkTasks(['bulk-a', 'bulk-b', 'bulk-c'], { status: 'todo' })
    expect(calls).toEqual([
      {
        path: '/tasks/bulk?board=ops',
        opts: { connectionId: 'local', profile: 'worker-a', method: 'POST', body: { ids: ['bulk-a'], status: 'todo' } }
      },
      {
        path: '/tasks/bulk?board=ops',
        opts: { connectionId: 'local', profile: 'worker-b', method: 'POST', body: { ids: ['bulk-b'], status: 'todo' } }
      },
      {
        path: '/tasks/bulk?board=other',
        opts: { connectionId: 'local', profile: 'worker-a', method: 'POST', body: { ids: ['bulk-c'], status: 'todo' } }
      }
    ])
  })

  it('keeps the single-gateway path byte-identical', async () => {
    registry('local')
    await bind(() => board({ ready: [task('a')] }))

    const result = await fetchBoard(false)

    expect(calls.filter(c => c.path.startsWith('/board'))).toEqual([{ opts: undefined, path: '/board' }])
    expect(result.columns[0].tasks[0].gateway).toBeUndefined()
  })

  it('reads every gateway and routes each write to the owner', async () => {
    registry('local', 'mini')
    await bind(call => {
      if (call.path.startsWith('/board?')) {
        return call.opts?.connectionId === 'mini'
          ? board({ ready: [task('remote1'), task('remote2')] })
          : board({ ready: [task('local1')] })
      }

      if (call.path.startsWith('/tasks/bulk')) {
        return { results: (call.opts?.body as { ids: string[] }).ids.map(id => ({ id, ok: true })) }
      }

      if (call.path.startsWith('/tasks?')) {
        return { task: { id: 'fresh', status: 'ready' } }
      }

      return {}
    })
    // After bind: it hydrates the slug from (empty) storage.
    $boardSlug.set('ops')
    await vi.waitFor(() => expect(calls.length).toBeGreaterThan(0))
    calls = []

    const merged = await fetchBoard(false)
    const reads = calls.filter(c => c.path.startsWith('/board?'))

    expect(reads.map(c => [c.path, c.opts?.connectionId])).toEqual([
      ['/board?board=ops', undefined],
      ['/board?board=ops', 'mini']
    ])
    expect(merged.columns[0].tasks.map(t => [t.id, t.gateway])).toEqual([
      ['local1', 'local'],
      ['remote1', 'mini'],
      ['remote2', 'mini']
    ])

    calls = []
    await patchTask('remote1', { status: 'todo' })
    await patchTask('local1', { status: 'todo' })
    expect(calls.map(c => [c.path, c.opts?.method, c.opts?.connectionId])).toEqual([
      ['/tasks/remote1?board=ops', 'PATCH', 'mini'],
      ['/tasks/local1?board=ops', 'PATCH', undefined]
    ])

    calls = []
    const bulk = await bulkTasks(['local1', 'remote1', 'remote2'], { status: 'done' })
    expect(bulk.results.map(r => r.id).sort()).toEqual(['local1', 'remote1', 'remote2'])
    expect(calls.map(c => [c.path, c.opts?.connectionId, (c.opts?.body as { ids: string[] }).ids])).toEqual(
      expect.arrayContaining([
        ['/tasks/bulk?board=ops', 'mini', ['remote1', 'remote2']],
        ['/tasks/bulk?board=ops', undefined, ['local1']]
      ])
    )
    expect(calls).toHaveLength(2)

    // A task created on another gateway routes its follow-up writes there
    // before any board read has seen it.
    calls = []
    await createTask({ title: 'x' }, 'mini')
    await patchTask('fresh', { status: 'triage' })
    expect(calls.map(c => [c.path, c.opts?.connectionId])).toEqual([
      ['/tasks?board=ops', 'mini'],
      ['/tasks/fresh?board=ops', 'mini']
    ])
  })

  it("resolves the '' board alias against the routed gateway for every gateway", async () => {
    registry('local', 'mini')
    await bind(call => {
      if (call.path === '/boards') {
        return { boards: [{ slug: 'work' }], current: 'work' }
      }

      return board({ ready: [] })
    })

    await fetchBoard(false)

    expect(calls.filter(c => c.path.startsWith('/board')).map(c => [c.path, c.opts?.connectionId])).toContainEqual([
      '/board?board=work',
      'mini'
    ])
    // The routed gateway keeps its unscoped read.
    expect(calls.filter(c => c.path.startsWith('/board')).map(c => [c.path, c.opts?.connectionId])).toContainEqual([
      '/board',
      undefined
    ])
  })

  // [daveey] Every board refetch (socket frame, poll, retry) used to read the
  // routed gateway's full `/boards` list (per-board counts) first, only to learn
  // `current`. On a slow link that doubled the hub reads: 297 `/boards`
  // timeouts in 30 min on 2026-11-13, paired 1:1 with `/board` reads.
  it("reads the routed gateway's /boards once for the '' alias, not before every board read", async () => {
    registry('local', 'mini')
    await bind(call =>
      call.path === '/boards' ? { boards: [{ slug: 'work' }], current: 'work' } : board({ ready: [] })
    )

    vi.useFakeTimers()
    await Promise.all([fetchBoard(false), fetchBoard(false), fetchBoard(true)])
    await fetchBoard(false)

    expect(calls.filter(c => c.path === '/boards')).toHaveLength(1)
    // Every remote read still names the routed gateway's current board.
    expect(
      calls
        .filter(c => c.opts?.connectionId === 'mini' && c.path.startsWith('/board?'))
        .every(c => c.path.includes('board=work'))
    ).toBe(true)

    // Past the boards list's own freshness window it is read again.
    await vi.advanceTimersByTimeAsync(31_000)
    await fetchBoard(false)
    expect(calls.filter(c => c.path === '/boards')).toHaveLength(2)
  })

  it('keeps the last known current board when the routed /boards read fails, and retries it next time', async () => {
    registry('local', 'mini')
    let down = false
    await bind(call => {
      if (call.path === '/boards') {
        return down
          ? new Error('Timed out connecting to Hermes backend after 20000ms')
          : { boards: [], current: 'work' }
      }

      return board({ ready: [] })
    })

    vi.useFakeTimers()
    await fetchBoard(false)
    await vi.advanceTimersByTimeAsync(31_000)
    down = true
    calls = []
    await fetchBoard(false)

    expect(calls.filter(c => c.path === '/boards')).toHaveLength(1)
    expect(calls).toContainEqual(
      expect.objectContaining({ path: '/board?board=work', opts: expect.objectContaining({ connectionId: 'mini' }) })
    )
  })

  it('does not let a hung gateway stall the board, and cools it down', async () => {
    registry('local', 'cloud')
    await bind(call =>
      call.opts?.connectionId === 'cloud'
        ? new Promise(() => undefined) // e.g. an OAuth login nobody completes
        : call.path === '/boards'
          ? { boards: [], current: 'default' }
          : board({ ready: [task('a')] })
    )

    vi.useFakeTimers()
    const pending = fetchBoard(false)
    await vi.advanceTimersByTimeAsync(6_000)
    const merged = await pending

    expect(merged.columns[0].tasks.map(t => t.id)).toEqual(['a'])
    expect(merged.gateway_errors).toEqual({ cloud: 'no answer in 6s' })

    // The next poll skips the cooled-down gateway instead of waiting again.
    calls = []
    const again = await fetchBoard(false)
    expect(again.gateway_errors).toEqual({ cloud: 'no answer in 6s' })
    expect(calls.some(c => c.opts?.connectionId === 'cloud')).toBe(false)
  })

  it('lists the union of boards from every gateway', async () => {
    registry('local', 'mini')
    await bind(call =>
      call.opts?.connectionId === 'mini'
        ? { boards: [{ slug: 'ops', total: 4 }], current: 'ops' }
        : { boards: [{ slug: 'default', total: 1 }], current: 'default' }
    )

    const boards = await fetchBoards()

    expect(boards.current).toBe('default')
    expect(boards.boards.map(meta => [meta.slug, meta.gateways])).toEqual([
      ['default', ['local']],
      ['ops', ['mini']]
    ])
  })
})
