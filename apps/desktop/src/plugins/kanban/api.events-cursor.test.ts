import { type PluginRestOptions, type PluginStorage, queryClient } from '@hermes/plugin-sdk'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Opening /events with no since replayed the board's whole task_events history.
// The socket must start at the snapshot's tail, and a late snapshot must not
// reopen a board the user already left. The cursor is per connection.

const { $boardSlug, bindApi, boardKey, EVENTS_BOARD_REFRESH_MIN_MS } = await import('./api')
const { setConnection } = await import('@/store/session')

const BOARD = (latest_event_id: number) => ({
  assignees: [],
  columns: [],
  latest_event_id,
  now: 0,
  tenants: []
})

const storage = (slug = ''): PluginStorage => ({
  get: <T>(key: string, fallback: T) => (key === 'boardSlug' ? (slug as T) : fallback),
  remove: vi.fn(),
  set: vi.fn()
})

afterEach(() => {
  setConnection(null)
  $boardSlug.set('')
  queryClient.clear()
})

describe('kanban event cursor', () => {
  it('does not open the socket until the board snapshot resolves, then starts at its tail', async () => {
    let resolveBoard: (board: ReturnType<typeof BOARD>) => void = () => undefined

    const board = new Promise<ReturnType<typeof BOARD>>(resolve => {
      resolveBoard = resolve
    })

    const rest = async <T>(path: string, _opts?: PluginRestOptions): Promise<T> => {
      if (path === '/board?board=ops') {
        return board as Promise<T>
      }

      throw new Error(`unexpected REST path: ${path}`)
    }

    const socket = vi.fn(() => vi.fn())
    const dispose = bindApi(rest, storage('ops'), socket)

    expect(socket).not.toHaveBeenCalled()
    resolveBoard(BOARD(14_386))

    await vi.waitFor(() => {
      expect(socket).toHaveBeenCalledWith('/events?board=ops&since=14386', expect.any(Function))
    })

    dispose()
  })

  it('seeds since from the cached snapshot instead of replaying history', () => {
    queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(14_386))

    const socket = vi.fn(() => vi.fn())

    const dispose = bindApi(
      async () => {
        throw new Error('cached snapshot must not refetch')
      },
      storage('ops'),
      socket
    )

    expect(socket).toHaveBeenCalledWith('/events?board=ops&since=14386', expect.any(Function))
    dispose()
  })

  it('drops a snapshot that resolves after the selected board changed', async () => {
    let resolveOps: (board: ReturnType<typeof BOARD>) => void = () => undefined

    const ops = new Promise<ReturnType<typeof BOARD>>(resolve => {
      resolveOps = resolve
    })

    const rest = async <T>(path: string, _opts?: PluginRestOptions): Promise<T> => {
      if (path === '/board?board=ops') {
        return ops as Promise<T>
      }

      if (path === '/board?board=ship') {
        return BOARD(7) as T
      }

      throw new Error(`unexpected REST path: ${path}`)
    }

    const socket = vi.fn((_path: string) => vi.fn())
    const dispose = bindApi(rest, storage('ops'), socket)

    expect(socket).not.toHaveBeenCalled()
    $boardSlug.set('ship')

    await vi.waitFor(() => {
      expect(socket).toHaveBeenCalledWith('/events?board=ship&since=7', expect.any(Function))
    })

    resolveOps(BOARD(14_386))
    await Promise.resolve()
    await Promise.resolve()

    expect(socket.mock.calls.map(([path]) => path)).toEqual(['/events?board=ship&since=7'])
    dispose()
  })

  it('resumes this connection from the last frame, and does not lend that cursor to another gateway', async () => {
    queryClient.setQueryData(boardKey('local', 'ship', false), BOARD(10))
    queryClient.setQueryData(boardKey('spark', 'ship', false), BOARD(4))

    const frames: Array<(data: unknown) => void> = []

    const socket = vi.fn((_path: string, onMessage: (data: unknown) => void) => {
      frames.push(onMessage)

      return vi.fn()
    })

    const dispose = bindApi(
      async () => {
        throw new Error('cached snapshots must not refetch')
      },
      {
        get: <T>(key: string, fallback: T) => (key.startsWith('boardSlug') ? ('ship' as T) : fallback),
        remove: vi.fn(),
        set: vi.fn()
      },
      socket
    )

    expect(socket).toHaveBeenCalledWith('/events?board=ship&since=10', expect.any(Function))
    frames.at(-1)!({ cursor: 25, events: [{ id: 25, kind: 'spawned', task_id: 't_1' }] })

    $boardSlug.set('ops')
    $boardSlug.set('ship')
    expect(socket.mock.calls.at(-1)?.[0]).toBe('/events?board=ship&since=25')

    setConnection({ connectionId: 'spark', mode: 'remote' } as never)
    await vi.waitFor(() => {
      expect(socket.mock.calls.at(-1)?.[0]).toBe('/events?board=ship&since=4')
    })

    dispose()
  })

  // [daveey] 2026-11-13: the hub's own /board read timed out, mergeBoards stamped the cached board
  // latest_event_id 0, and the socket opened at since=0: a full history replay, 200 events a frame.
  it('never opens at since=0 from a snapshot whose routed gateway did not answer', async () => {
    queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(0))

    const socket = vi.fn(() => vi.fn())

    const dispose = bindApi(
      async () => {
        throw new Error('hub /board timed out again')
      },
      storage('ops'),
      socket
    )

    await vi.waitFor(() => {
      expect(socket).toHaveBeenCalledWith('/events?board=ops', expect.any(Function))
    })
    expect(socket.mock.calls.flat().some(arg => String(arg).includes('since=0'))).toBe(false)
    dispose()
  })

  it('opens at the server tail when the fetched snapshot carries no positive cursor', async () => {
    const socket = vi.fn(() => vi.fn())
    const dispose = bindApi(async <T>() => BOARD(0) as T, storage('ops'), socket)

    await vi.waitFor(() => {
      expect(socket).toHaveBeenCalledWith('/events?board=ops', expect.any(Function))
    })

    dispose()
  })

  it('coalesces a burst of frames into one immediate and one trailing board re-read', async () => {
    vi.useFakeTimers()

    try {
      queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(10))
      const frames: Array<(data: unknown) => void> = []

      const dispose = bindApi(
        async () => {
          throw new Error('cached snapshot must not refetch')
        },
        storage('ops'),
        vi.fn((_path: string, onMessage: (data: unknown) => void) => {
          frames.push(onMessage)

          return vi.fn()
        })
      )

      const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
      const boardReads = () => invalidate.mock.calls.filter(([filters]) => filters?.queryKey?.[1] === 'board').length

      for (let id = 11; id <= 60; id++) {
        frames.at(-1)!({ cursor: id, events: [{ id, kind: 'heartbeat', task_id: `t_${id}` }] })
      }

      expect(boardReads()).toBe(1)
      // A read in flight answers the next frame too: no orphaned duplicate request.
      expect(invalidate.mock.calls.find(([filters]) => filters?.queryKey?.[1] === 'board')?.[1]).toEqual({
        cancelRefetch: false
      })

      vi.advanceTimersByTime(EVENTS_BOARD_REFRESH_MIN_MS)
      expect(boardReads()).toBe(2)

      vi.advanceTimersByTime(10 * EVENTS_BOARD_REFRESH_MIN_MS)
      expect(boardReads()).toBe(2)

      invalidate.mockRestore()
      dispose()
    } finally {
      vi.useRealTimers()
    }
  })
})
