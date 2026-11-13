import { type PluginRestOptions, type PluginStorage, queryClient } from '@hermes/plugin-sdk'
import { QueryObserver } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

// [daveey] A board read slower than the board poll interval must not stack polls. On 2026-11-13 board reads queued
// 30 s behind a saturated socket pool; every overlapping read would have been one more request in that queue. The
// page and the count pill poll the same key (boardPoll), the socket opens at that key's snapshot, and live frames
// refresh it only after the read in flight settles, so there is at most one board read in flight at a time.
// (completion-notify's one-time per-board baseline read is not a poll; it is stubbed out here.)
vi.mock('./completion-notify', () => ({
  bindCompletionNotify: vi.fn(),
  onKanbanEventsFrame: vi.fn(async () => false)
}))

const { $boardSlug, bindApi, boardPoll, boardPollMs, fetchBoard } = await import('./api')
const { setConnection } = await import('@/store/session')

const BOARD = { assignees: [], columns: [], latest_event_id: 7, now: 0, tenants: [] }

const storage = (slug: string): PluginStorage => ({
  get: <T>(key: string, fallback: T) => (key === 'boardSlug' ? (slug as T) : fallback),
  remove: vi.fn(),
  set: vi.fn()
})

afterEach(() => {
  vi.useRealTimers()
  setConnection(null)
  $boardSlug.set('')
  queryClient.clear()
})

describe('kanban board poll', () => {
  it('a slow board read does not start overlapping polls', async () => {
    vi.useFakeTimers()

    const boardReads: Array<(board: typeof BOARD) => void> = []
    let onFrame: (data: unknown) => void = () => undefined

    const rest = async <T>(path: string, _opts?: PluginRestOptions): Promise<T> => {
      if (path === '/board?board=ops') {
        // Answers only when the test says so: far slower than the poll interval.
        return new Promise<T>(resolve => boardReads.push(board => resolve(board as T)))
      }

      throw new Error(`unexpected REST path: ${path}`)
    }

    const socket = vi.fn((_path: string, handler: (data: unknown) => void) => {
      onFrame = handler

      return vi.fn()
    })

    const dispose = bindApi(rest, storage('ops'), socket)

    // The board page (fast poll) and the count pill (slow poll) on the same board.
    const page = new QueryObserver(queryClient, {
      ...boardPoll('local', 'ops', false, boardPollMs(true)),
      queryFn: () => fetchBoard(false)
    })

    const pill = new QueryObserver(queryClient, {
      ...boardPoll('local', 'ops', false, boardPollMs(false)),
      queryFn: () => fetchBoard(false)
    })

    const unsubscribe = [page.subscribe(() => undefined), pill.subscribe(() => undefined)]

    try {
      await vi.advanceTimersByTimeAsync(0)
      expect(boardReads).toHaveLength(1)

      // Four of the page's poll intervals and one of the pill's pass while the first read is still out.
      await vi.advanceTimersByTimeAsync(boardPollMs(true) * 4 + 1)
      expect(boardReads).toHaveLength(1)

      // It answers: the socket opens at its tail, and the next interval tick reads once more.
      boardReads[0](BOARD)
      await vi.advanceTimersByTimeAsync(0)
      expect(socket).toHaveBeenCalledWith('/events?board=ops&since=7', expect.any(Function))
      await vi.advanceTimersByTimeAsync(boardPollMs(true))
      expect(boardReads).toHaveLength(2)

      // A live frame while that second read is slow waits for it instead of starting a parallel read.
      onFrame({ cursor: 8, events: [{ task_id: 't1' }] })
      await vi.advanceTimersByTimeAsync(boardPollMs(true) * 2 + 1)
      expect(boardReads).toHaveLength(2)

      // Once it settles, the deferred refresh reads exactly once.
      boardReads[1](BOARD)
      await vi.advanceTimersByTimeAsync(0)
      expect(boardReads).toHaveLength(3)
    } finally {
      unsubscribe.forEach(stop => stop())
      dispose()
    }
  })
})
