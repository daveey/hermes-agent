import { describe, expect, it, vi } from 'vitest'

import { type TranscriptCacheKey, TranscriptCacheStore } from './transcript-cache'
import {
  GATEWAY_BACKOFF_MS,
  type HubSessionRecord,
  MIN_REFETCH_MS,
  selectPrefetchTargets,
  TranscriptPrefetcher
} from './transcript-prefetch'

const CONNECTIONS: Record<string, string> = { hub: 'hermes-hub', macbook: 'local', mini: 'mac-mini' }

const record = (gateway: string, id: string, activity: number, extra: Partial<HubSessionRecord> = {}) => ({
  activity,
  gateway,
  id,
  message_count: 10,
  profile: 'default',
  ...extra
})

describe('prefetch selection', () => {
  it('takes the most recently active tips per gateway, skipping archived, hidden, parents and unregistered gateways', () => {
    const records: HubSessionRecord[] = [
      record('mini', 'old', 1),
      record('mini', 'new', 5),
      record('mini', 'mid', 3),
      record('mini', 'gone', 9, { archived: true }),
      record('mini', 'secret', 9, { hidden: true }),
      record('hub', 'root', 4),
      record('hub', 'tip', 6, { parent_session_id: 'root' }),
      record('hub', 'bot', 8, { automated: true, profile: 'alpha' }),
      record('elsewhere', 'x', 10)
    ]

    const targets = selectPrefetchTargets(records, gateway => CONNECTIONS[gateway], 2)

    expect(targets.map(target => `${target.key.connectionId}/${target.key.profile}/${target.key.sessionId}`)).toEqual([
      // people's chats first, newest first; automations after
      'hermes-hub/default/tip',
      'mac-mini/default/new',
      'mac-mini/default/mid',
      'hermes-hub/alpha/bot'
    ])
  })

  it('[daveey] puts the sessions of open Inbox cards first, even automations past a full chat window', () => {
    const records = [
      ...Array.from({ length: 5 }, (_, index) => record('hub', `chat${index}`, 100 + index)),
      record('hub', 'selfheal', 1, { automated: true, inbox: true }),
      record('hub', 'cron', 50, { automated: true })
    ]

    const targets = selectPrefetchTargets(records, gateway => CONNECTIONS[gateway], 3)

    expect(targets.map(target => target.key.sessionId)).toEqual(['selfheal', 'chat4', 'chat3'])
    expect(targets[0].inbox).toBe(true)
  })

  it('[daveey] prefetches a hidden or archived session when an open Inbox card points at it, and only then', () => {
    const records = [
      record('hub', 'card', 1, { hidden: true, inbox: true }),
      record('hub', 'archived-card', 2, { archived: true, inbox: true }),
      record('hub', 'hidden-chat', 9, { hidden: true }),
      record('hub', 'chat', 5)
    ]

    const targets = selectPrefetchTargets(records, gateway => CONNECTIONS[gateway])

    expect(targets.map(target => target.key.sessionId)).toEqual(['archived-card', 'card', 'chat'])
  })

  it('caps each gateway separately', () => {
    const records = [
      ...Array.from({ length: 250 }, (_, index) => record('mini', `m${index}`, index)),
      ...Array.from({ length: 250 }, (_, index) => record('hub', `h${index}`, index))
    ]

    const targets = selectPrefetchTargets(records, gateway => CONNECTIONS[gateway])

    expect(targets.filter(target => target.key.connectionId === 'mac-mini')).toHaveLength(200)
    expect(targets.filter(target => target.key.connectionId === 'hermes-hub')).toHaveLength(200)
    expect(targets.some(target => target.key.sessionId === 'm49')).toBe(false)
    expect(targets.some(target => target.key.sessionId === 'm50')).toBe(true)
  })

  it("keeps a gateway's people chats in the window when automations are busier", () => {
    const records = [
      ...Array.from({ length: 250 }, (_, index) => record('hub', `bot${index}`, 1000 + index, { automated: true })),
      record('hub', 'quiet-chat', 1)
    ]

    const hub = selectPrefetchTargets(records, gateway => CONNECTIONS[gateway])

    expect(hub).toHaveLength(200)
    expect(hub[0].key.sessionId).toBe('quiet-chat')
  })
})

describe('prefetch refresh on activity change', () => {
  function harness(records: HubSessionRecord[]) {
    let clock = 1_000_000
    const store = new TranscriptCacheStore(':memory:', { now: () => clock })
    const fetchLatest = vi.fn(async (key: TranscriptCacheKey) => [{ content: `${key.sessionId}@${clock}`, id: clock }])

    const prefetcher = new TranscriptPrefetcher({
      connectionForGateway: gateway => CONNECTIONS[gateway],
      fetchLatest,
      listHubSessions: async () => records,
      now: () => clock,
      store
    })

    return {
      advance: (ms: number) => (clock += ms),
      fetchLatest,
      prefetcher,
      store
    }
  }

  it('fetches every target once, then only the session whose activity or message_count moved', async () => {
    const records = [record('mini', 'a', 1), record('mini', 'b', 2), record('hub', 'c', 3)]
    const h = harness(records)

    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 3, targets: 3 })
    expect(h.store.read({ connectionId: 'mac-mini', profile: 'default', sessionId: 'a' })?.activity).toBe(1)

    h.advance(MIN_REFETCH_MS)
    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 0, skipped: 3 })

    records[1] = record('mini', 'b', 7) // new activity on b
    records[2] = record('hub', 'c', 3, { message_count: 11 }) // a message landed on c
    h.fetchLatest.mockClear()
    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 2 })
    expect(h.fetchLatest.mock.calls.map(([key]) => key.sessionId).sort()).toEqual(['b', 'c'])
    expect(h.store.read({ connectionId: 'mac-mini', profile: 'default', sessionId: 'b' })?.activity).toBe(7)
  })

  it('refetches a session that keeps moving at most once per MIN_REFETCH_MS', async () => {
    const records = [record('mini', 'a', 1)]
    const h = harness(records)

    await h.prefetcher.tick()
    records[0] = record('mini', 'a', 2)
    h.advance(1_000)
    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 0 })
    h.advance(MIN_REFETCH_MS)
    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 1 })
  })

  it('backs off a gateway whose route fails without blocking the others', async () => {
    const records = [record('mini', 'a', 1), record('mini', 'b', 2), record('mini', 'c', 3), record('hub', 'd', 4)]
    const h = harness(records)

    h.fetchLatest.mockImplementation(async key => {
      if (key.connectionId === 'mac-mini') {
        throw new Error('connect ECONNREFUSED')
      }

      return [{ content: 'ok', id: 1 }]
    })

    const first = await h.prefetcher.tick()
    expect(first.fetched).toBe(1)
    expect(first.failed).toBeGreaterThanOrEqual(1)
    // At most the two in-flight workers hit the dead route before it is marked down.
    expect(h.fetchLatest.mock.calls.filter(([key]) => key.connectionId === 'mac-mini').length).toBeLessThanOrEqual(2)

    h.fetchLatest.mockClear()
    await h.prefetcher.tick()
    expect(h.fetchLatest.mock.calls.some(([key]) => key.connectionId === 'mac-mini')).toBe(false)

    h.advance(GATEWAY_BACKOFF_MS)
    await h.prefetcher.tick()
    expect(h.fetchLatest.mock.calls.some(([key]) => key.connectionId === 'mac-mini')).toBe(true)
  })

  it('a 404 for one session does not mark its gateway down', async () => {
    const h = harness([record('mini', 'a', 1), record('mini', 'b', 2), record('mini', 'c', 3)])

    h.fetchLatest.mockImplementation(async key => {
      if (key.sessionId === 'c') {
        throw Object.assign(new Error('not found'), { status: 404 })
      }

      return [{ content: 'ok', id: 1 }]
    })

    expect(await h.prefetcher.tick()).toMatchObject({ failed: 1, fetched: 2 })
  })

  // An empty session (a fresh cron/kanban tip: message_count > 0 in the replica, nothing displayable yet) or a
  // 404 stores nothing, so the cache had no row and every 5 s tick fetched it again: ~1100 transcript GETs per
  // 90 s across three gateways on David-MBP (t_01a18977df212d21).
  it('does not refetch an empty or missing session until its activity or message_count moves', async () => {
    const records = [record('mini', 'empty', 1), record('mini', 'gone', 2), record('mini', 'full', 3)]
    const h = harness(records)

    h.fetchLatest.mockImplementation(async key => {
      if (key.sessionId === 'gone') {
        throw Object.assign(new Error('not found'), { status: 404 })
      }

      return key.sessionId === 'empty' ? [] : [{ content: 'ok', id: 1 }]
    })

    await h.prefetcher.tick()
    expect(h.fetchLatest).toHaveBeenCalledTimes(3)

    h.fetchLatest.mockClear()
    h.advance(MIN_REFETCH_MS)
    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 0, skipped: 3 })
    expect(h.fetchLatest).not.toHaveBeenCalled()

    records[0] = record('mini', 'empty', 9) // the session moved: try again
    await h.prefetcher.tick()
    expect(h.fetchLatest.mock.calls.map(([key]) => key.sessionId)).toEqual(['empty'])
  })

  it('does nothing without a hub replica', async () => {
    const store = new TranscriptCacheStore(':memory:')
    const fetchLatest = vi.fn()

    const prefetcher = new TranscriptPrefetcher({
      connectionForGateway: () => 'local',
      fetchLatest,
      listHubSessions: async () => null,
      store
    })

    expect(await prefetcher.tick()).toMatchObject({ fetched: 0, targets: 0 })
    expect(fetchLatest).not.toHaveBeenCalled()
  })
})
