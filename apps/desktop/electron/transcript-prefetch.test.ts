import { describe, expect, it, vi } from 'vitest'

import { type TranscriptCacheKey, TranscriptCacheStore } from './transcript-cache'
import {
  DEMAND_CONCURRENCY,
  DEMAND_QUEUE_MAX,
  GATEWAY_BACKOFF_MS,
  type HubSessionRecord,
  MIN_REFETCH_MS,
  selectPrefetchTargets,
  TranscriptPrefetcher,
  UNLISTED_REFETCH_MS
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

describe('[daveey] on-demand prefetch of an @session-referenced session', () => {
  function harness(records: HubSessionRecord[] = []) {
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

    return { advance: (ms: number) => (clock += ms), fetchLatest, prefetcher, store }
  }

  const old = { connectionId: 'hermes-hub', profile: 'default', sessionId: '20261018_114650_7ed013' }

  it('fetches a session outside the hub listing into the cache, then only once the copy is UNLISTED_REFETCH_MS old', async () => {
    const h = harness()

    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('fetched')
    expect(h.store.read(old)?.messages).toHaveLength(1)
    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('current')

    // Old sessions don't move: a hover after MIN_REFETCH_MS is still served by the cache.
    h.advance(MIN_REFETCH_MS)
    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('current')
    expect(h.fetchLatest).toHaveBeenCalledTimes(1)

    h.advance(UNLISTED_REFETCH_MS)
    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('fetched')
  })

  it('a store that throws (closed at quit) resolves failed instead of throwing out of the IPC listener', async () => {
    const h = harness()
    h.store.close()

    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('failed')
    expect(h.fetchLatest).not.toHaveBeenCalled()
  })

  it('dedupes a key already queued or in flight', async () => {
    const h = harness()
    const first = h.prefetcher.prefetchKey(old)

    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('queued')
    await expect(first).resolves.toBe('fetched')
    expect(h.fetchLatest).toHaveBeenCalledTimes(1)
  })

  it('uses the hub meta from the last pass: unchanged is current, moved is refetched', async () => {
    // Hidden: listed by the hub but never a tick target, so only the on-demand path fetches it.
    const records = [record('hub', 'listed', 5, { hidden: true })]
    const h = harness(records)
    const key = { connectionId: 'hermes-hub', profile: 'default', sessionId: 'listed' }

    await h.prefetcher.tick()
    expect(h.fetchLatest).not.toHaveBeenCalled()
    await expect(h.prefetcher.prefetchKey(key)).resolves.toBe('fetched')
    expect(h.store.meta(key)).toMatchObject({ activity: 5, messageCount: 10 })

    h.advance(MIN_REFETCH_MS)
    await expect(h.prefetcher.prefetchKey(key)).resolves.toBe('current')

    records[0] = record('hub', 'listed', 9, { hidden: true })
    await h.prefetcher.tick()
    await expect(h.prefetcher.prefetchKey(key)).resolves.toBe('fetched')
    expect(h.fetchLatest).toHaveBeenCalledTimes(2)
    expect(h.store.meta(key)).toMatchObject({ activity: 9 })
  })

  it('backs off a gateway that failed (shared with the tick) and runs at most DEMAND_CONCURRENCY per gateway', async () => {
    const h = harness()
    let inFlight = 0
    let peak = 0

    h.fetchLatest.mockImplementation(async key => {
      inFlight += 1
      peak = Math.max(peak, inFlight)
      await new Promise(resolve => setTimeout(resolve, 5))
      inFlight -= 1

      if (key.connectionId === 'mac-mini') {
        throw new Error('connect ECONNREFUSED')
      }

      return [{ content: 'ok', id: 1 }]
    })

    const keys = Array.from({ length: 6 }, (_, index) => ({ ...old, sessionId: `s${index}` }))
    await expect(Promise.all(keys.map(key => h.prefetcher.prefetchKey(key)))).resolves.toEqual(Array(6).fill('fetched'))
    expect(peak).toBeLessThanOrEqual(DEMAND_CONCURRENCY)

    const dead = { connectionId: 'mac-mini', profile: 'default', sessionId: 'x' }
    await expect(h.prefetcher.prefetchKey(dead)).resolves.toBe('failed')
    h.fetchLatest.mockClear()
    await expect(h.prefetcher.prefetchKey({ ...dead, sessionId: 'y' })).resolves.toBe('backoff')
    expect(h.fetchLatest).not.toHaveBeenCalled()

    h.advance(GATEWAY_BACKOFF_MS)
    await expect(h.prefetcher.prefetchKey({ ...dead, sessionId: 'y' })).resolves.toBe('failed')
  })

  it('does not refetch a missing session within MIN_REFETCH_MS, and rejects a key without an owner', async () => {
    const h = harness()
    h.fetchLatest.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))

    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('failed')
    await expect(h.prefetcher.prefetchKey(old)).resolves.toBe('current')
    await expect(h.prefetcher.prefetchKey({ ...old, connectionId: '' })).resolves.toBe('invalid')
    expect(h.fetchLatest).toHaveBeenCalledTimes(1)
  })

  it('drops keys past DEMAND_QUEUE_MAX queued on one gateway', async () => {
    const h = harness()
    const gate = deferredRows()
    h.fetchLatest.mockImplementation(() => gate.promise)

    const keys = Array.from({ length: DEMAND_CONCURRENCY + DEMAND_QUEUE_MAX + 1 }, (_, index) => ({
      ...old,
      sessionId: `q${index}`
    }))

    const outcomes = keys.map(key => h.prefetcher.prefetchKey(key))

    await expect(outcomes[outcomes.length - 1]).resolves.toBe('dropped')
    gate.resolve([{ content: 'ok', id: 1 }])
    const settled = await Promise.all(outcomes)
    expect(settled.filter(outcome => outcome === 'dropped')).toHaveLength(1)
    expect(h.fetchLatest).toHaveBeenCalledTimes(DEMAND_CONCURRENCY + DEMAND_QUEUE_MAX)
  })
})

function deferredRows() {
  let resolve!: (rows: { content: string; id: number }[]) => void
  const promise = new Promise<{ content: string; id: number }[]>(done => (resolve = done))

  return { promise, resolve }
}

describe('[daveey] prefetch over an asynchronous store (the worker-thread cache)', () => {
  function harness(records: HubSessionRecord[]) {
    const clock = 1_000_000
    const inner = new TranscriptCacheStore(':memory:', { now: () => clock })
    const later = <T>(value: () => T) => new Promise<T>(resolve => setTimeout(() => resolve(value()), 1))

    const store = {
      meta: vi.fn((key: TranscriptCacheKey) => later(() => inner.meta(key))),
      metaMany: vi.fn((keys: readonly TranscriptCacheKey[]) => later(() => keys.map(key => inner.meta(key)))),
      putLatest: vi.fn((key: TranscriptCacheKey, latest: readonly unknown[], meta?: object) =>
        later(() => inner.putLatest(key, latest, meta))
      )
    }

    const fetchLatest = vi.fn(async (key: TranscriptCacheKey) => [{ content: key.sessionId, id: 1 }])

    const prefetcher = new TranscriptPrefetcher({
      connectionForGateway: gateway => CONNECTIONS[gateway],
      fetchLatest,
      listHubSessions: async () => records,
      now: () => clock,
      store
    })

    return { fetchLatest, inner, prefetcher, store }
  }

  it('checks a whole pass in one batched meta read and awaits each write before counting it', async () => {
    const h = harness([record('mini', 'a', 1), record('mini', 'b', 2), record('hub', 'c', 3)])

    expect(await h.prefetcher.tick()).toMatchObject({ failed: 0, fetched: 3, targets: 3 })
    expect(h.store.metaMany).toHaveBeenCalledTimes(1)
    expect(h.store.meta).not.toHaveBeenCalled()
    expect(h.inner.stats().sessions).toBe(3)

    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 0, skipped: 3 })
    expect(h.fetchLatest).toHaveBeenCalledTimes(3)
  })

  it('a write the store refuses (resolves false) is not counted as fetched', async () => {
    const h = harness([record('mini', 'a', 1)])
    h.store.putLatest.mockResolvedValue(false)

    expect(await h.prefetcher.tick()).toMatchObject({ fetched: 0, skipped: 1 })
  })

  it('asks for one key while its freshness check is still in flight share that check: one fetch', async () => {
    const h = harness([])
    const key = { connectionId: 'mac-mini', profile: 'default', sessionId: 'ref' }
    const asks = [h.prefetcher.prefetchKey(key), h.prefetcher.prefetchKey(key), h.prefetcher.prefetchKey(key)]

    expect(await Promise.all(asks)).toEqual(['fetched', 'queued', 'queued'])
    expect(h.store.meta).toHaveBeenCalledTimes(1)
    expect(h.fetchLatest).toHaveBeenCalledTimes(1)
    await expect(h.prefetcher.prefetchKey(key)).resolves.toBe('current')
  })

  it('a store that fails the freshness check reports failed and leaves the key askable again', async () => {
    const h = harness([])
    const key = { connectionId: 'mac-mini', profile: 'default', sessionId: 'ref' }
    h.store.meta.mockRejectedValueOnce(new Error('transcript cache worker lost (exit 1)'))

    expect(await Promise.all([h.prefetcher.prefetchKey(key), h.prefetcher.prefetchKey(key)])).toEqual([
      'failed',
      'failed'
    ])
    expect(h.fetchLatest).not.toHaveBeenCalled()
    await expect(h.prefetcher.prefetchKey(key)).resolves.toBe('fetched')
  })
})
