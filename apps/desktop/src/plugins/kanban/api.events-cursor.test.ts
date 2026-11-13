import { type PluginRestOptions, type PluginStorage, queryClient } from '@hermes/plugin-sdk'
import { QueryObserver } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Opening /events with no since replayed the board's whole task_events history.
// The socket must start at the snapshot's tail, and a late snapshot must not
// reopen a board the user already left. The cursor is per connection.

const { $boardSlug, bindApi, blockedKey, boardKey, EVENTS_BOARD_REFRESH_MIN_MS } = await import('./api')
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

  // [daveey] A read slower than EVENTS_BOARD_REFRESH_MIN_MS (hub tunnel) used to absorb the trailing
  // refresh: cancelRefetch: false joined the in-flight promise and its success cleared isInvalidated,
  // so the frames after its server snapshot waited for the 15 s/60 s poll.
  it('re-reads the board once after a read that a trailing refresh found still in flight', async () => {
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

      const reads: Array<{ resolve: (board: ReturnType<typeof BOARD>) => void; signal: AbortSignal }> = []

      const observer = new QueryObserver(queryClient, {
        queryFn: ({ signal }) =>
          new Promise<ReturnType<typeof BOARD>>(resolve => {
            reads.push({ resolve, signal })
          }),
        queryKey: boardKey('local', 'ops', false),
        staleTime: Infinity
      })

      const unsubscribe = observer.subscribe(() => undefined)
      const frame = (id: number) => frames.at(-1)!({ cursor: id, events: [{ id, kind: 'heartbeat', task_id: 't_1' }] })

      frame(11)
      await vi.advanceTimersByTimeAsync(0)
      expect(reads).toHaveLength(1)

      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS / 2)
      frame(12)
      // The trailing refresh fires while the first read is still on the wire: it neither cancels nor duplicates it.
      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(1)
      expect(reads[0].signal.aborted).toBe(false)

      reads[0].resolve(BOARD(11))
      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(2)

      reads[1].resolve(BOARD(12))
      await vi.advanceTimersByTimeAsync(10 * EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(2)

      unsubscribe()
      dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  // [daveey] #2289 follow-up (t_01a1d595575df431): the coalescing state is per binding. A disposed binding's trailing
  // refresh must not fire into the next one, and a fresh binding's first frame re-reads at once.
  const bindWithFrames = () => {
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

    const frame = (id: number) =>
      frames.at(-1)!({ cursor: id, events: [{ id, kind: 'heartbeat', task_id: `t_${id}` }] })

    return { dispose, frame }
  }

  const spyInvalidate = () => vi.spyOn(queryClient, 'invalidateQueries')

  const boardInvalidations = (spy: ReturnType<typeof spyInvalidate>) =>
    spy.mock.calls.filter(([filters]) => filters?.queryKey?.[1] === 'board').length

  it('drops a pending trailing board refresh when the binding is disposed', () => {
    vi.useFakeTimers()

    try {
      queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(10))
      const binding = bindWithFrames()
      const invalidate = spyInvalidate()

      binding.frame(11)
      binding.frame(12) // inside the interval: one trailing refresh is now pending
      expect(boardInvalidations(invalidate)).toBe(1)

      binding.dispose()
      vi.advanceTimersByTime(10 * EVENTS_BOARD_REFRESH_MIN_MS)
      expect(boardInvalidations(invalidate)).toBe(1)

      invalidate.mockRestore()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-reads the board on the first frame of a new binding even inside the last binding interval', () => {
    vi.useFakeTimers()

    try {
      queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(10))
      // A binding the host never disposed (a re-register over a live one) must not hand its interval on either.
      const first = bindWithFrames()
      const invalidate = spyInvalidate()

      first.frame(11)
      expect(boardInvalidations(invalidate)).toBe(1)

      const second = bindWithFrames()

      second.frame(12)
      expect(boardInvalidations(invalidate)).toBe(2)

      invalidate.mockRestore()
      second.dispose()
      first.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  // [daveey] #2336 follow-ups (t_01a1d595575df431, from t_01a1d59498376333): the in-flight wait is one per key, it
  // restarts the interval, and the Blocked section read is never cancelled either.
  const slowReads = (queryKey: readonly unknown[]) => {
    const reads: Array<{ resolve: (value: unknown) => void; signal: AbortSignal }> = []

    const observer = new QueryObserver(queryClient, {
      queryFn: ({ signal }) =>
        new Promise<unknown>(resolve => {
          reads.push({ resolve, signal })
        }),
      queryKey,
      staleTime: Infinity
    })

    return { reads, unsubscribe: observer.subscribe(() => undefined) }
  }

  it('waits on a slow board read once however many trailing refreshes land during it', async () => {
    vi.useFakeTimers()

    try {
      queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(10))
      const binding = bindWithFrames()
      const { reads, unsubscribe } = slowReads(boardKey('local', 'ops', false))

      binding.frame(11)
      await vi.advanceTimersByTimeAsync(1000)
      binding.frame(12)
      await vi.advanceTimersByTimeAsync(2000)
      binding.frame(13)
      await vi.advanceTimersByTimeAsync(4000)
      expect(reads).toHaveLength(1)

      reads[0].resolve(BOARD(11))
      await vi.advanceTimersByTimeAsync(10 * EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(2)

      reads[1].resolve(BOARD(13))
      await vi.advanceTimersByTimeAsync(10 * EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(2)

      unsubscribe()
      binding.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('starts the interval over when a deferred re-read begins', async () => {
    vi.useFakeTimers()

    try {
      queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(10))
      const binding = bindWithFrames()
      const { reads, unsubscribe } = slowReads(boardKey('local', 'ops', false))

      binding.frame(11)
      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS / 2)
      binding.frame(12)
      // The trailing refresh fires at MIN_MS and waits; the slow read outlives the whole interval.
      await vi.advanceTimersByTimeAsync(2 * EVENTS_BOARD_REFRESH_MIN_MS)
      reads[0].resolve(BOARD(11))
      await vi.advanceTimersByTimeAsync(0)
      expect(reads).toHaveLength(2)
      reads[1].resolve(BOARD(12))
      await vi.advanceTimersByTimeAsync(0)

      binding.frame(13)
      await vi.advanceTimersByTimeAsync(0)
      expect(reads).toHaveLength(2)

      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(3)

      unsubscribe()
      binding.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('neither cancels nor duplicates a Blocked section read still in flight', async () => {
    vi.useFakeTimers()

    try {
      queryClient.setQueryData(boardKey('local', 'ops', false), BOARD(10))
      const binding = bindWithFrames()
      const { reads, unsubscribe } = slowReads(blockedKey('local'))

      binding.frame(11)
      await vi.advanceTimersByTimeAsync(0)
      expect(reads).toHaveLength(1)

      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS / 2)
      binding.frame(12)
      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(1)
      expect(reads[0].signal.aborted).toBe(false)

      reads[0].resolve({ tasks: [] })
      await vi.advanceTimersByTimeAsync(EVENTS_BOARD_REFRESH_MIN_MS)
      expect(reads).toHaveLength(2)

      unsubscribe()
      binding.dispose()
    } finally {
      vi.useRealTimers()
    }
  })
})
