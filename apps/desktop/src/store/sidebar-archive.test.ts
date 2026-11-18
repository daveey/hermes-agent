import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $apiRequestScope } from '@/api/client'
import type { SessionInfo } from '@/types/hermes'

import { $sidebarShowArchived, $sidebarShowAutomated } from './layout'
import {
  $archivedSessions,
  $archivedSessionsHasMore,
  $archivedSessionsLoading,
  createArchivedPager,
  loadArchivedSessions,
  loadMoreArchivedSessions
} from './sidebar-archive'

const listAllProfileSessions = vi.hoisted(() => vi.fn())

vi.mock('@/api/sessions', () => ({
  listAllProfileSessions
}))

const PAGE_SIZE = 500

const row = (id: string, overrides: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    id,
    archived: true,
    input_tokens: 0,
    output_tokens: 0,
    message_count: 1,
    started_at: 1,
    last_active: 1,
    source: 'desktop',
    tool_call_count: 0,
    ...overrides
  }) as SessionInfo

/** A back-filled pinned row: appended past the window by the server, ignores
 * the archived filter, and repeats on every page whose window covers it. */
const pinnedRow = (id: string): SessionInfo => row(id, { archived: false, pinned: true })

/** `id`-numbered archived rows for a full page of the given size. */
const page = (from: number, count: number): SessionInfo[] =>
  Array.from({ length: count }, (_, i) => row(`a-${from + i}`))

/** Read a cursor to its end, as a caller that wants the whole archive would. */
async function listEveryArchivedSession(): Promise<SessionInfo[]> {
  const pager = createArchivedPager(true)

  while (pager.hasMore) {
    await pager.next()
  }

  return pager.sessions
}

let scopeSerial = 0

beforeEach(() => {
  $sidebarShowArchived.set(false)
  // Automated rows included unless a test says otherwise: the plain `{}` filter.
  $sidebarShowAutomated.set(true)
  // A fresh scope per test: the switch drops the previous test's cursor and page count.
  $apiRequestScope.set({ connectionId: `reset-${(scopeSerial += 1)}`, profile: null })
  $apiRequestScope.set({ connectionId: null, profile: null })
  $archivedSessions.set([])
  listAllProfileSessions.mockReset()
})

describe('loadArchivedSessions', () => {
  it('keeps same-id archived sessions from different profiles across pages', async () => {
    listAllProfileSessions
      .mockResolvedValueOnce({
        sessions: [row('same', { profile: 'default' }), ...page(1, PAGE_SIZE - 1)],
        total: PAGE_SIZE + 1
      })
      .mockResolvedValueOnce({ sessions: [row('same', { profile: 'work' })], total: PAGE_SIZE + 1 })

    const sessions = await listEveryArchivedSession()

    expect(sessions.filter(session => session.id === 'same').map(session => session.profile)).toEqual([
      'default',
      'work'
    ])
  })

  it('keeps the last successful result when a refresh fails', async () => {
    const existing = { id: 'archived-1', title: 'Keep me' } as SessionInfo
    $archivedSessions.set([existing])
    listAllProfileSessions.mockRejectedValue(new Error('offline'))

    await loadArchivedSessions()

    expect($archivedSessions.get()).toEqual([existing])
  })

  it('does not publish the previous connection rows after a switch mid-load', async () => {
    let resolvePrevious!: (page: unknown) => void
    let resolveCurrent!: (page: unknown) => void

    listAllProfileSessions
      .mockImplementationOnce(() => new Promise(resolve => (resolvePrevious = resolve)))
      .mockImplementationOnce(() => new Promise(resolve => (resolveCurrent = resolve)))

    $apiRequestScope.setKey('connectionId', 'conn-a')
    const previous = loadArchivedSessions()
    $apiRequestScope.setKey('connectionId', 'conn-b')
    const current = loadArchivedSessions()

    // The new connection's reload must fetch, not be swallowed by A's flag.
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)

    resolveCurrent({ sessions: [row('b-archived')], total: 1 })
    await current
    resolvePrevious({ sessions: [row('a-archived')], total: 1 })
    await previous

    expect($archivedSessions.get().map(session => session.id)).toEqual(['b-archived'])
    expect($archivedSessionsLoading.get()).toBe(false)
  })

  it('clears the previous connection rows when a load starts on a new one', async () => {
    let resolveCurrent!: (page: unknown) => void

    listAllProfileSessions
      .mockResolvedValueOnce({ sessions: [row('a-archived')], total: 1 })
      .mockImplementationOnce(() => new Promise(resolve => (resolveCurrent = resolve)))

    $apiRequestScope.setKey('connectionId', 'conn-a')
    await loadArchivedSessions()
    $apiRequestScope.setKey('connectionId', 'conn-b')
    const current = loadArchivedSessions()

    expect($archivedSessions.get()).toEqual([])

    resolveCurrent({ sessions: [row('b-archived')], total: 1 })
    await current

    expect($archivedSessions.get().map(session => session.id)).toEqual(['b-archived'])
  })

  // Nothing else reloads on a scope switch: the sidebar effect watches only the Archived toggle
  // and background sync waits for a backend sessions.changed event.
  it('clears and reloads an open Archived view on a scope switch with no other trigger', async () => {
    $sidebarShowArchived.set(true)
    let resolveCurrent!: (page: unknown) => void

    listAllProfileSessions
      .mockResolvedValueOnce({ sessions: [row('a-archived')], total: 1 })
      .mockImplementationOnce(() => new Promise(resolve => (resolveCurrent = resolve)))

    await loadArchivedSessions()
    $apiRequestScope.setKey('profile', 'work')

    expect($archivedSessions.get()).toEqual([])
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)

    resolveCurrent({ sessions: [row('work-archived')], total: 1 })
    await vi.waitFor(() => expect($archivedSessions.get().map(session => session.id)).toEqual(['work-archived']))
    expect($archivedSessionsLoading.get()).toBe(false)
  })

  it('replaces a first load stranded by a scope switch while Archived is open', async () => {
    $sidebarShowArchived.set(true)
    let resolvePrevious!: (page: unknown) => void

    listAllProfileSessions
      .mockImplementationOnce(() => new Promise(resolve => (resolvePrevious = resolve)))
      .mockResolvedValueOnce({ sessions: [row('b-archived')], total: 1 })

    const previous = loadArchivedSessions()
    $apiRequestScope.setKey('connectionId', 'conn-b')

    await vi.waitFor(() => expect($archivedSessions.get().map(session => session.id)).toEqual(['b-archived']))
    resolvePrevious({ sessions: [row('a-archived')], total: 1 })
    await previous

    expect($archivedSessions.get().map(session => session.id)).toEqual(['b-archived'])
    expect($archivedSessionsLoading.get()).toBe(false)
  })

  it('clears a closed Archived view on a scope switch without fetching', async () => {
    $archivedSessions.set([row('a-archived')])

    $apiRequestScope.setKey('profile', 'work')

    expect($archivedSessions.get()).toEqual([])
    expect(listAllProfileSessions).not.toHaveBeenCalled()
  })
})

describe('archived view paging', () => {
  // 2026-11-17: 16,899 archived rows across three gateways. Reading all of them before
  // publishing any (34 pages, each slower than the last) left the view empty.
  it('shows the first page of a deep archive without reading the rest', async () => {
    listAllProfileSessions.mockResolvedValue({ sessions: page(0, PAGE_SIZE), total: 16_899 })

    await loadArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)
    expect($archivedSessions.get()).toHaveLength(PAGE_SIZE)
    expect($archivedSessionsHasMore.get()).toBe(true)
    expect($archivedSessionsLoading.get()).toBe(false)
  })

  it('reports nothing more when the first page holds the whole archive', async () => {
    listAllProfileSessions.mockResolvedValue({ sessions: page(0, 3), total: 3 })

    await loadArchivedSessions()

    expect($archivedSessionsHasMore.get()).toBe(false)
    await loadMoreArchivedSessions()
    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)
  })

  it('appends one page per load-more, from the next offset', async () => {
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: page(args[6] as number, PAGE_SIZE),
      total: 3 * PAGE_SIZE
    }))

    await loadArchivedSessions()
    await loadMoreArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect(listAllProfileSessions).toHaveBeenNthCalledWith(2, PAGE_SIZE, 0, 'only', 'recent', 'all', {}, PAGE_SIZE)
    expect($archivedSessions.get().map(session => session.id)).toEqual(page(0, 2 * PAGE_SIZE).map(s => s.id))
    expect($archivedSessionsHasMore.get()).toBe(true)

    await loadMoreArchivedSessions()

    expect($archivedSessions.get()).toHaveLength(3 * PAGE_SIZE)
    expect($archivedSessionsHasMore.get()).toBe(false)
  })

  it('re-reads every page the user loaded on a refresh', async () => {
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: page(args[6] as number, PAGE_SIZE),
      total: 4 * PAGE_SIZE
    }))

    await loadArchivedSessions()
    await loadMoreArchivedSessions()
    listAllProfileSessions.mockClear()

    await loadArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect($archivedSessions.get()).toHaveLength(2 * PAGE_SIZE)
    expect($archivedSessionsHasMore.get()).toBe(true)
  })

  it('keeps the loaded rows and the offer when a load-more fails', async () => {
    listAllProfileSessions
      .mockResolvedValueOnce({ sessions: page(0, PAGE_SIZE), total: 2 * PAGE_SIZE })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ sessions: page(PAGE_SIZE, PAGE_SIZE), total: 2 * PAGE_SIZE })

    await loadArchivedSessions()
    await loadMoreArchivedSessions()

    expect($archivedSessions.get()).toHaveLength(PAGE_SIZE)
    expect($archivedSessionsHasMore.get()).toBe(true)
    expect($archivedSessionsLoading.get()).toBe(false)

    await loadMoreArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenLastCalledWith(PAGE_SIZE, 0, 'only', 'recent', 'all', {}, PAGE_SIZE)
    expect($archivedSessions.get()).toHaveLength(2 * PAGE_SIZE)
  })

  // Review of the first cut: archive/unarchive/delete edit $archivedSessions in place.
  it('reads again from the top when the list was edited before a load-more', async () => {
    const server = page(0, 3 * PAGE_SIZE)

    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: server.slice(args[6] as number, (args[6] as number) + PAGE_SIZE),
      total: server.length
    }))

    await loadArchivedSessions()
    // What unarchiving a-0 does: the row leaves the list here and on the server.
    $archivedSessions.set($archivedSessions.get().filter(session => session.id !== 'a-0'))
    server.shift()
    listAllProfileSessions.mockClear()

    await loadMoreArchivedSessions()

    const ids = $archivedSessions.get().map(session => session.id)

    expect(listAllProfileSessions).toHaveBeenNthCalledWith(1, PAGE_SIZE, 0, 'only', 'recent', 'all', {}, 0)
    expect(ids).not.toContain('a-0')
    // a-500 moved into the first window; the old cursor would have skipped it.
    expect(ids).toEqual(server.slice(0, 2 * PAGE_SIZE).map(session => session.id))
  })

  it('reads again from the top when the list is edited while a load-more is reading', async () => {
    const server = page(0, 3 * PAGE_SIZE)
    let release!: () => void

    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: server.slice(args[6] as number, (args[6] as number) + PAGE_SIZE),
      total: server.length
    }))
    await loadArchivedSessions()

    listAllProfileSessions.mockImplementationOnce(async () => {
      const rows = server.slice(PAGE_SIZE, 2 * PAGE_SIZE)

      await new Promise<void>(resolve => (release = resolve))

      return { sessions: rows, total: server.length + 1 }
    })

    const more = loadMoreArchivedSessions()

    await vi.waitFor(() => expect(release).toBeTypeOf('function'))
    $archivedSessions.set($archivedSessions.get().filter(session => session.id !== 'a-0'))
    server.shift()
    release()
    await more

    expect($archivedSessions.get().map(session => session.id)).toEqual(
      server.slice(0, 2 * PAGE_SIZE).map(session => session.id)
    )
    expect($archivedSessionsLoading.get()).toBe(false)
  })

  // sessions.changed refreshes can run back to back; a click must not be lost to one.
  it('has a running refresh read the extra page when load-more is asked mid-refresh', async () => {
    let release!: () => void

    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: page(args[6] as number, PAGE_SIZE),
      total: 4 * PAGE_SIZE
    }))
    await loadArchivedSessions()

    listAllProfileSessions.mockImplementationOnce(async () => {
      await new Promise<void>(resolve => (release = resolve))

      return { sessions: page(0, PAGE_SIZE), total: 4 * PAGE_SIZE }
    })

    const refresh = loadArchivedSessions()

    await vi.waitFor(() => expect(release).toBeTypeOf('function'))

    const more = loadMoreArchivedSessions()
    let moreDone = false
    void more.then(() => (moreDone = true))

    await Promise.resolve()
    expect(moreDone).toBe(false)

    release()
    await Promise.all([refresh, more])

    expect($archivedSessions.get()).toHaveLength(2 * PAGE_SIZE)
    expect($archivedSessionsLoading.get()).toBe(false)
  })

  it('does not count a load-more stranded by a scope round trip against the scope it returns to', async () => {
    let release!: () => void

    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: page(args[6] as number, PAGE_SIZE),
      total: 4 * PAGE_SIZE
    }))
    await loadArchivedSessions()

    listAllProfileSessions.mockImplementationOnce(async () => {
      await new Promise<void>(resolve => (release = resolve))

      return { sessions: page(PAGE_SIZE, PAGE_SIZE), total: 4 * PAGE_SIZE }
    })

    const more = loadMoreArchivedSessions()

    await vi.waitFor(() => expect(release).toBeTypeOf('function'))
    $apiRequestScope.setKey('profile', 'work')
    $apiRequestScope.setKey('profile', null)
    release()
    await more

    expect($archivedSessions.get()).toEqual([])
    expect($archivedSessionsLoading.get()).toBe(false)

    await loadArchivedSessions()

    expect($archivedSessions.get()).toHaveLength(PAGE_SIZE)
  })

  it('drops the cursor on a scope switch, so the new scope starts at its first page', async () => {
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: page(args[6] as number, PAGE_SIZE),
      total: 4 * PAGE_SIZE
    }))

    await loadArchivedSessions()
    await loadMoreArchivedSessions()
    $apiRequestScope.setKey('profile', 'work')

    expect($archivedSessionsHasMore.get()).toBe(false)
    listAllProfileSessions.mockClear()
    await loadMoreArchivedSessions()
    expect(listAllProfileSessions).not.toHaveBeenCalled()

    await loadArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)
    expect($archivedSessions.get()).toHaveLength(PAGE_SIZE)
  })
})

// 2026-11-17: 15,432 of 16,899 archived rows were bot runs the sidebar then hid; 92 of the
// first 500 were the user's own.
describe('archived view and automated rows', () => {
  it('asks for pages without automated rows while they are hidden', async () => {
    $sidebarShowAutomated.set(false)
    listAllProfileSessions.mockResolvedValue({ sessions: page(0, 2), total: 2 })

    await loadArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenCalledWith(
      PAGE_SIZE,
      0,
      'only',
      'recent',
      'all',
      { automated: 'exclude' },
      0
    )
  })

  it('asks for every row while automated sessions are shown', async () => {
    listAllProfileSessions.mockResolvedValue({ sessions: page(0, 2), total: 2 })

    await loadArchivedSessions()

    expect(listAllProfileSessions).toHaveBeenCalledWith(PAGE_SIZE, 0, 'only', 'recent', 'all', {}, 0)
  })

  it('starts the open view over when the preference flips, back at one page', async () => {
    $sidebarShowArchived.set(true)
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => ({
      sessions: page(args[6] as number, PAGE_SIZE),
      total: 4 * PAGE_SIZE
    }))
    await loadArchivedSessions()
    await loadMoreArchivedSessions()
    listAllProfileSessions.mockClear()

    $sidebarShowAutomated.set(false)

    expect($archivedSessions.get()).toEqual([])
    await vi.waitFor(() => expect($archivedSessions.get()).toHaveLength(PAGE_SIZE))
    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)
    expect(listAllProfileSessions).toHaveBeenCalledWith(
      PAGE_SIZE,
      0,
      'only',
      'recent',
      'all',
      { automated: 'exclude' },
      0
    )
  })

  it('drops the loaded rows without fetching when the preference flips with the view closed', async () => {
    listAllProfileSessions.mockResolvedValue({ sessions: page(0, 2), total: 2 })
    await loadArchivedSessions()
    listAllProfileSessions.mockClear()

    $sidebarShowAutomated.set(false)

    expect($archivedSessions.get()).toEqual([])
    expect(listAllProfileSessions).not.toHaveBeenCalled()
  })
})

describe('createArchivedPager read to the end', () => {
  it('keeps paging when a live pin displaced the last archived row', async () => {
    listAllProfileSessions
      .mockResolvedValueOnce({ sessions: [pinnedRow('pin-x'), ...page(0, PAGE_SIZE - 1)], total: PAGE_SIZE })
      .mockResolvedValueOnce({ sessions: [row(`a-${PAGE_SIZE - 1}`), pinnedRow('pin-x')], total: PAGE_SIZE })

    const sessions = await listEveryArchivedSession()

    expect(sessions.filter(session => session.archived)).toHaveLength(PAGE_SIZE)
    expect(sessions.filter(session => session.id === 'pin-x')).toHaveLength(1)
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
  })

  it('stops past the total when only live pins arrive', async () => {
    // A row archived mid-load behind the cursor leaves the count one short,
    // and the backend still back-fills the live pin past the end.
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => {
      const offset = args[6] as number

      if (offset > 4 * PAGE_SIZE) {
        throw new Error('archived paging did not stop')
      }

      return { sessions: [...page(offset, Math.max(0, PAGE_SIZE - offset)), pinnedRow('pin-x')], total: PAGE_SIZE + 1 }
    })

    const sessions = await listEveryArchivedSession()

    expect(sessions.filter(session => session.archived)).toHaveLength(PAGE_SIZE)
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
  })

  it('paginates until the backend total is loaded', async () => {
    const firstPage = Array.from({ length: PAGE_SIZE }, (_, i) => row(`a${i}`))

    listAllProfileSessions
      .mockResolvedValueOnce({ sessions: firstPage, total: PAGE_SIZE + 1, limit: PAGE_SIZE, offset: 0 })
      .mockResolvedValueOnce({ sessions: [row('c')], total: PAGE_SIZE + 1, limit: PAGE_SIZE, offset: PAGE_SIZE })

    await expect(listEveryArchivedSession()).resolves.toEqual([...firstPage, row('c')])
    expect(listAllProfileSessions).toHaveBeenNthCalledWith(1, PAGE_SIZE, 0, 'only', 'recent', 'all', {}, 0)
    expect(listAllProfileSessions).toHaveBeenNthCalledWith(2, PAGE_SIZE, 0, 'only', 'recent', 'all', {}, PAGE_SIZE)
  })

  it('stops on an empty page when profiles changed during pagination', async () => {
    const firstPage = Array.from({ length: PAGE_SIZE }, (_, i) => row(`a${i}`))

    listAllProfileSessions
      .mockResolvedValueOnce({ sessions: firstPage, total: 2 * PAGE_SIZE, limit: PAGE_SIZE, offset: 0 })
      .mockResolvedValueOnce({ sessions: [], total: 2 * PAGE_SIZE, limit: PAGE_SIZE, offset: PAGE_SIZE })

    await expect(listEveryArchivedSession()).resolves.toEqual(firstPage)
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
  })

  it('advances the offset by the page size, not by the row count', async () => {
    // Server contract: a page returns limit window rows PLUS every back-filled
    // pinned row, so pages 1 and 2 here both hand back 200 archived rows and
    // a riding pin. The next offset must stay 200/400 regardless.
    listAllProfileSessions
      .mockResolvedValueOnce({
        sessions: [...page(0, PAGE_SIZE), pinnedRow('pin-x')],
        total: 2 * PAGE_SIZE + 1,
        limit: PAGE_SIZE,
        offset: 0
      })
      .mockResolvedValueOnce({
        sessions: [...page(PAGE_SIZE, PAGE_SIZE), pinnedRow('pin-x')],
        total: 2 * PAGE_SIZE + 1,
        limit: PAGE_SIZE,
        offset: PAGE_SIZE
      })
      .mockResolvedValueOnce({
        sessions: [row(`a-${2 * PAGE_SIZE}`)],
        total: 2 * PAGE_SIZE + 1,
        limit: PAGE_SIZE,
        offset: 2 * PAGE_SIZE
      })

    const sessions = await listEveryArchivedSession()

    // Every archived row plus the pin — none skipped, pin listed once.
    expect(sessions).toHaveLength(2 * PAGE_SIZE + 2)
    expect(sessions.filter(s => s.id === 'pin-x')).toHaveLength(1)
    expect(sessions.filter(s => s.archived).map(s => s.id)).toContain(`a-${2 * PAGE_SIZE}`)
    expect(listAllProfileSessions).toHaveBeenNthCalledWith(2, PAGE_SIZE, 0, 'only', 'recent', 'all', {}, PAGE_SIZE)
    expect(listAllProfileSessions).toHaveBeenNthCalledWith(3, PAGE_SIZE, 0, 'only', 'recent', 'all', {}, 2 * PAGE_SIZE)
  })

  it('loads every archived row when non-archived pins inflate each page', async () => {
    // Reviewer's worst case: 1000 archived rows; pins live outside the archive
    // and total counts only archived rows. pages 1-5 are full 200-row windows
    // each carrying 100 extra pin rows, so the row count crosses 1000 on page
    // 5 — the loop must still stop on the cursor, not on the inflated count.
    const total = 5 * PAGE_SIZE
    let calls = 0
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => {
      const offset = args[6] as number
      calls += 1
      const remaining = Math.max(0, Math.min(PAGE_SIZE, total - offset))
      const pins = Array.from({ length: 100 }, (_, i) => pinnedRow(`pin-${i}`))

      return {
        sessions: [...page(offset, remaining), ...pins],
        total,
        limit: PAGE_SIZE,
        offset
      }
    })

    const sessions = await listEveryArchivedSession()

    // 5 pages — the 6th request is never issued because the cursor has
    // reached total.
    expect(calls).toBe(5)
    expect(sessions.filter(s => s.archived)).toHaveLength(total)
    // The 100 distinct pins ride along once each, not once per page.
    expect(sessions.filter(s => !s.archived)).toHaveLength(100)
  })

  it('reads a deep archive in backend-sized pages so the merge path re-reads its prefix fewer times', async () => {
    // Review on #140972: the Electron merge path answers each page by reading
    // the whole prefix through offset + limit, so the page size sets how many
    // times that prefix is re-read. 650 archived rows: two requests, not four.
    const total = 650
    listAllProfileSessions.mockImplementation(async (...args: unknown[]) => {
      const [limit, , , , , , offset] = args as [number, number, string, string, string, object, number]

      return { sessions: page(offset, Math.max(0, Math.min(limit, total - offset))), total, limit, offset }
    })

    const sessions = await listEveryArchivedSession()

    expect(sessions).toHaveLength(total)
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect(listAllProfileSessions).toHaveBeenNthCalledWith(1, 500, 0, 'only', 'recent', 'all', {}, 0)
  })
})
