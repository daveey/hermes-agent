/**
 * [daveey] Push-driven transcript prefetch (`sessions.moved`): delta merge, fallbacks, coalescing, the listing
 * safety-net cadence and older gateways. Runs against the real SQLite cache store (in memory) and a fake gateway
 * that serves the same rows the REST routes would.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { type TranscriptCacheKey, TranscriptCacheStore } from './transcript-cache'
import {
  DELTA_REPROBE_MS,
  type DeltaPage,
  EARLY_PASS_MIN_MS,
  type HubSessionRecord,
  type MovedSession,
  type PrefetchDeps,
  PUSH_LIVE_MS,
  SAFETY_NET_MS,
  TranscriptPrefetcher,
  UNKNOWN_IGNORE_MS
} from './transcript-prefetch'

interface Row {
  content: string
  id: number
  role: string
}

/** One gateway's sessions as the REST routes serve them. */
class FakeGateway {
  rows = new Map<string, Row[]>()
  revision = new Map<string, number>()
  /** A session whose reads resolve to another stored id (a compression rotation). */
  resolve = new Map<string, string>()
  nextId = 1
  latestCalls: string[] = []
  afterCalls: { afterId: number; sessionId: string }[] = []
  rowsServed = 0
  afterSupported = true
  foldConflict = false
  gate: null | Promise<void> = null

  append(sessionId: string, count = 1): MovedSession {
    const rows = this.rows.get(sessionId) ?? []

    for (let index = 0; index < count; index += 1) {
      rows.push({ content: `${sessionId} #${this.nextId}`, id: this.nextId++, role: 'assistant' })
    }

    this.rows.set(sessionId, rows)

    return this.moved(sessionId)
  }

  moved(sessionId: string, extra: Partial<MovedSession> = {}): MovedSession {
    const rows = this.rows.get(sessionId) ?? []

    return {
      activity: rows.length,
      id: sessionId,
      last_message_id: rows.at(-1)?.id ?? null,
      message_count: rows.length,
      profile: 'default',
      ...extra
    }
  }

  // Both reads snapshot the store when the request arrives; `gate` holds only the reply (a slow link).
  async fetchLatest(key: TranscriptCacheKey, limit: number): Promise<unknown[]> {
    this.latestCalls.push(key.sessionId)
    const rows = (this.rows.get(this.resolve.get(key.sessionId) ?? key.sessionId) ?? []).slice(-limit)
    this.rowsServed += rows.length
    await this.gate

    return rows.map(row => ({ ...row }))
  }

  async fetchAfter(key: TranscriptCacheKey, afterId: number, limit: number): Promise<DeltaPage | null> {
    this.afterCalls.push({ afterId, sessionId: key.sessionId })

    if (!this.afterSupported) {
      await this.gate

      return null
    }

    const sessionId = this.resolve.get(key.sessionId) ?? key.sessionId
    const rows = this.rows.get(sessionId) ?? []
    const anchorFound = rows.some(row => row.id === afterId)
    const after = anchorFound ? rows.filter(row => row.id > afterId) : []
    const page = after.slice(0, limit)
    this.rowsServed += page.length
    const revision = this.revision.get(sessionId) ?? 0
    await this.gate

    return {
      anchorFound,
      foldConflict: this.foldConflict,
      hasMore: after.length > limit,
      messages: page.map(row => ({ ...row })),
      sessionId,
      transcriptRevision: revision
    }
  }
}

const CONNECTION = 'hermes-hub'
const keyFor = (sessionId: string): TranscriptCacheKey => ({ connectionId: CONNECTION, profile: 'default', sessionId })

function setup(options: { records?: () => HubSessionRecord[] | null; withDelta?: boolean } = {}) {
  let now = 1_000_000
  const gateway = new FakeGateway()
  const store = new TranscriptCacheStore(':memory:', { now: () => now })
  const listings: number[] = []

  const deps: PrefetchDeps = {
    connectionForGateway: name => (name === 'hub' ? CONNECTION : undefined),
    fetchLatest: (key, limit) => gateway.fetchLatest(key, limit),
    listHubSessions: async () => {
      listings.push(now)

      return options.records?.() ?? []
    },
    now: () => now,
    store: {
      meta: key => store.meta(key),
      metaMany: keys => keys.map(key => store.meta(key)),
      putAfter: (key, anchorId, rows, meta) => store.putAfter(key, anchorId, rows, meta),
      putLatest: (key, rows, meta) => store.putLatest(key, rows, meta),
      tailId: key => store.tailId(key)
    }
  }

  if (options.withDelta !== false) {
    deps.fetchAfter = (key, afterId, limit) => gateway.fetchAfter(key, afterId, limit)
  }

  const prefetcher = new TranscriptPrefetcher(deps)

  return {
    advance: (ms: number) => {
      now += ms
    },
    cachedIds: (sessionId: string) =>
      (store.read(keyFor(sessionId))?.messages ?? []).map(message => (message as Row).id),
    gateway,
    listings,
    now: () => now,
    prefetcher,
    push: (...sessions: MovedSession[]) => prefetcher.sessionsMoved(CONNECTION, sessions),
    store
  }
}

/** Seed a cached session whose revision the prefetcher has learnt (its first push refetched the page). */
async function warm(env: ReturnType<typeof setup>, sessionId: string, rows = 3) {
  env.gateway.append(sessionId, rows)
  env.store.putLatest(keyFor(sessionId), await env.gateway.fetchLatest(keyFor(sessionId), 200))
  await env.push(env.gateway.append(sessionId))
  env.gateway.latestCalls.length = 0
  env.gateway.afterCalls.length = 0
}

afterEach(() => {
  vi.useRealTimers()
})

describe('push delta merge', () => {
  it('appends only the rows after the cached tail, with no latest-page fetch', async () => {
    const env = setup()
    await warm(env, 'chat')
    const before = env.cachedIds('chat')

    await env.push(env.gateway.append('chat', 2))

    expect(env.gateway.latestCalls).toEqual([])
    expect(env.gateway.afterCalls).toEqual([{ afterId: before.at(-1), sessionId: 'chat' }])
    expect(env.cachedIds('chat')).toEqual((env.gateway.rows.get('chat') ?? []).map(row => row.id))
    expect(env.store.meta(keyFor('chat'))).toMatchObject({ messageCount: 6 })
    expect(env.prefetcher.pushStats.delta).toBe(1)
  })

  it('learns the revision on the first push of a cached session and refetches that once', async () => {
    const env = setup()
    env.gateway.append('chat', 3)
    env.store.putLatest(keyFor('chat'), await env.gateway.fetchLatest(keyFor('chat'), 200))
    env.gateway.latestCalls.length = 0

    await env.push(env.gateway.append('chat'))
    await env.push(env.gateway.append('chat'))

    // The cache predates anything the prefetcher saw: one latest page, then deltas.
    expect(env.gateway.latestCalls).toEqual(['chat'])
    expect(env.prefetcher.pushStats).toMatchObject({ delta: 1, full: 1 })
    expect(env.cachedIds('chat')).toEqual((env.gateway.rows.get('chat') ?? []).map(row => row.id))
  })

  it('skips a push whose rows the cache already holds', async () => {
    const env = setup()
    await warm(env, 'chat')

    await env.push(env.gateway.moved('chat'))

    expect(env.gateway.afterCalls).toEqual([])
    expect(env.prefetcher.pushStats.current).toBe(1)
  })
})

describe('push fallbacks to the latest page', () => {
  it.each([
    [
      'an in-place edit (revision moved)',
      (gateway: FakeGateway) => {
        gateway.rows.get('chat')![0].content = 'edited'
        gateway.revision.set('chat', 1)
      }
    ],
    [
      'a compaction or rewind (the anchor row is gone)',
      (gateway: FakeGateway) => {
        gateway.rows.set('chat', gateway.rows.get('chat')!.slice(0, -1))
      }
    ],
    [
      'a gap (more rows than one page)',
      (gateway: FakeGateway) => {
        gateway.append('chat', 250)
      }
    ],
    [
      'a row the page would fold into an earlier slot',
      (gateway: FakeGateway) => {
        gateway.append('chat')
        gateway.foldConflict = true
      }
    ],
    [
      'a rotation to a new stored id',
      (gateway: FakeGateway) => {
        gateway.rows.set('chat-child', [{ content: 'continued', id: gateway.nextId++, role: 'user' }])
        gateway.resolve.set('chat', 'chat-child')
      }
    ]
  ])('%s', async (_name, change) => {
    const env = setup()
    await warm(env, 'chat')

    change(env.gateway)
    await env.push(env.gateway.moved('chat', { last_message_id: env.gateway.nextId }))

    expect(env.gateway.latestCalls).toEqual(['chat'])
    expect(env.prefetcher.pushStats.full).toBe(2) // the warm-up's first page, then this fallback
    const served = await env.gateway.fetchLatest(keyFor('chat'), 200)
    expect(env.store.read(keyFor('chat'))?.messages).toEqual(served)
  })

  it('a cache rewritten under the delta (an open stored a newer page) refetches instead of stitching', async () => {
    const env = setup()
    await warm(env, 'chat')
    const moved = env.gateway.append('chat')
    const original = env.store.putAfter.bind(env.store)

    // The open's latest page lands between the delta read and its apply.
    env.store.putAfter = (key, anchor, rows, meta) => {
      env.store.putLatest(key, [{ content: 'local', id: 'client-row', role: 'user' }])

      return original(key, anchor, rows, meta)
    }

    await env.push(moved)

    expect(env.gateway.latestCalls).toEqual(['chat'])
  })
})

describe('push coalescing', () => {
  it('keeps one fetch in flight and one queued per session, the queued one the newest', async () => {
    const env = setup()
    await warm(env, 'chat')
    let release!: () => void
    env.gateway.gate = new Promise(resolve => (release = resolve))

    const first = env.push(env.gateway.append('chat'))
    await vi.waitFor(() => expect(env.gateway.afterCalls).toHaveLength(1)) // its read is on the wire
    const pushes = [first, ...[2, 3, 4, 5].map(() => env.push(env.gateway.append('chat')))]
    await new Promise(resolve => setTimeout(resolve, 0))
    release()
    env.gateway.gate = null
    await Promise.all(pushes)

    expect(env.gateway.afterCalls).toHaveLength(2)
    expect(env.prefetcher.pushStats.coalesced).toBe(3)
    expect(env.cachedIds('chat')).toEqual((env.gateway.rows.get('chat') ?? []).map(row => row.id))
  })

  it('runs different sessions side by side', async () => {
    const env = setup()
    await warm(env, 'a')
    await warm(env, 'b')

    await env.push(env.gateway.append('a'), env.gateway.append('b'))

    expect(env.gateway.afterCalls.map(call => call.sessionId).sort()).toEqual(['a', 'b'])
  })
})

describe('tracked set', () => {
  const covering = () => [{ activity: 1, gateway: 'hub', id: 'listed', message_count: 1 }] as HubSessionRecord[]

  it('ignores an unknown session but lists early (at most every EARLY_PASS_MIN_MS) so it is fetched', async () => {
    vi.useFakeTimers()
    const env = setup({ records: covering })
    env.prefetcher.start(5_000)
    await vi.advanceTimersByTimeAsync(0)
    const listingsBefore = env.listings.length

    await env.push(env.gateway.append('stranger'))
    await env.push(env.gateway.append('stranger'))

    expect(env.gateway.afterCalls).toEqual([])
    expect(env.prefetcher.pushStats.unknown).toBe(2)
    await vi.advanceTimersByTimeAsync(EARLY_PASS_MIN_MS)
    expect(env.prefetcher.pushStats.earlyPasses).toBe(1)
    expect(env.listings.length).toBeGreaterThan(listingsBefore)
    env.prefetcher.stop()
  })

  it('asks no early pass for a mover outside every listed (connection, profile) scope', async () => {
    vi.useFakeTimers()
    const env = setup({ records: covering })
    env.prefetcher.start(5_000)
    await vi.advanceTimersByTimeAsync(0)

    await env.push({ ...env.gateway.append('bot'), profile: 'alpha' }) // no listed session is on profile alpha

    await vi.advanceTimersByTimeAsync(EARLY_PASS_MIN_MS)
    expect(env.prefetcher.pushStats.earlyPasses).toBe(0)
    env.prefetcher.stop()
  })

  it('a mover an early pass did not pick up asks for no further early pass for UNKNOWN_IGNORE_MS', async () => {
    vi.useFakeTimers()
    const env = setup({ records: covering })
    env.prefetcher.start(60_000)
    await vi.advanceTimersByTimeAsync(0)

    // An automation past the listing window: every push used to re-arm an early pass on a busy hub.
    await env.push(env.gateway.append('automation'))
    await vi.advanceTimersByTimeAsync(EARLY_PASS_MIN_MS)
    expect(env.prefetcher.pushStats.earlyPasses).toBe(1)

    for (let index = 0; index < 5; index += 1) {
      await env.push(env.gateway.append('automation'))
      await vi.advanceTimersByTimeAsync(EARLY_PASS_MIN_MS)
    }

    expect(env.prefetcher.pushStats.earlyPasses).toBe(1)
    env.prefetcher.stop()
    expect(UNKNOWN_IGNORE_MS).toBe(600_000)
  })

  it('treats a compression child of a cached session as tracked', async () => {
    const env = setup()
    await warm(env, 'chat')
    env.gateway.rows.set('chat-child', [{ content: 'continued', id: env.gateway.nextId++, role: 'user' }])

    await env.push(env.gateway.moved('chat-child', { parent_session_id: 'chat' }))

    expect(env.gateway.latestCalls).toEqual(['chat-child'])
    expect(env.cachedIds('chat-child')).toHaveLength(1)
  })

  it('tracks a listing target that is not cached yet', async () => {
    const env = setup({ records: () => [{ activity: 1, gateway: 'hub', id: 'listed', message_count: 1 }] })
    env.gateway.append('listed')
    await env.prefetcher.tick()
    env.store.drop(keyFor('listed')) // (fetched by the pass; dropped to prove the target set alone tracks it)
    env.gateway.latestCalls.length = 0

    await env.push(env.gateway.append('listed'))

    expect(env.gateway.latestCalls).toEqual(['listed'])
  })
})

describe('listing safety-net cadence', () => {
  const records = () => [{ activity: 1, gateway: 'hub', id: 'chat', message_count: 1 }] as HubSessionRecord[]

  it('lists every tick for a gateway that never pushes (older core)', async () => {
    const env = setup({ records })

    for (let tick = 0; tick < 12; tick += 1) {
      await env.prefetcher.tick()
      env.advance(5_000)
    }

    expect(env.listings).toHaveLength(12)
  })

  it('drops to one listing per SAFETY_NET_MS while the gateway pushes, and back to every tick when pushes stop', async () => {
    const env = setup({ records })
    env.gateway.append('chat')
    await env.prefetcher.tick() // caches 'chat'
    await env.push(env.gateway.append('chat'))
    const start = env.listings.length

    // Two minutes of 5 s ticks while pushes keep arriving.
    for (let tick = 0; tick < 24; tick += 1) {
      env.advance(5_000)
      await env.push(env.gateway.append('chat'))
      await env.prefetcher.tick()
    }

    expect(env.listings.length - start).toBe(2) // 120 s / SAFETY_NET_MS

    // Pushes stop: once the connection is no longer push-live, every tick lists again.
    env.advance(PUSH_LIVE_MS)
    const quiet = env.listings.length

    for (let tick = 0; tick < 4; tick += 1) {
      await env.prefetcher.tick()
      env.advance(5_000)
    }

    expect(env.listings.length - quiet).toBe(4)
    expect(SAFETY_NET_MS).toBe(60_000)
  })

  it('still fetches a never-cached target of a push-live gateway on a pass between safety nets', async () => {
    let list: HubSessionRecord[] = [{ activity: 1, gateway: 'hub', id: 'old', message_count: 1 }]
    const env = setup({ records: () => list })
    env.gateway.append('old')
    await env.prefetcher.tick()
    await env.push(env.gateway.append('old'))
    env.gateway.latestCalls.length = 0

    env.gateway.append('new')
    list = [...list, { activity: 2, gateway: 'hub', id: 'new', message_count: 1 }]
    env.advance(5_000)
    await env.prefetcher.tick({ force: true }) // the early pass an unknown push asks for

    expect(env.gateway.latestCalls).toEqual(['new'])
  })
})

describe('older gateway', () => {
  it('without the after_id route a push refetches the latest page and stops asking for deltas', async () => {
    const env = setup()
    env.gateway.afterSupported = false
    env.gateway.append('chat', 2)
    env.store.putLatest(keyFor('chat'), await env.gateway.fetchLatest(keyFor('chat'), 200))
    env.gateway.latestCalls.length = 0

    await env.push(env.gateway.append('chat'))
    await env.push(env.gateway.append('chat'))

    expect(env.gateway.afterCalls).toHaveLength(1)
    expect(env.gateway.latestCalls).toEqual(['chat', 'chat'])
    expect(env.cachedIds('chat')).toEqual((env.gateway.rows.get('chat') ?? []).map(row => row.id))
  })

  it('asks a gateway without the route again after DELTA_REPROBE_MS (an upgrade mid-session gets deltas)', async () => {
    const env = setup()
    env.gateway.afterSupported = false
    await warm(env, 'chat')
    await env.push(env.gateway.append('chat'))
    expect(env.gateway.afterCalls).toHaveLength(0) // still within the window the warm-up's probe opened

    env.gateway.afterSupported = true
    env.advance(DELTA_REPROBE_MS)
    await env.push(env.gateway.append('chat')) // probes again: learns the revision (page)
    await env.push(env.gateway.append('chat'))

    expect(env.prefetcher.pushStats.delta).toBe(1)
    expect(env.cachedIds('chat')).toEqual((env.gateway.rows.get('chat') ?? []).map(row => row.id))
  })

  it('a renderer with no delta dep at all (older main wiring) still refetches pages', async () => {
    const env = setup({ withDelta: false })
    await warm(env, 'chat')

    await env.push(env.gateway.append('chat'))

    expect(env.gateway.latestCalls).toEqual(['chat'])
  })
})

describe('measurement: one streaming turn', () => {
  /**
   * A 60 s turn appends a row every 500 ms. Before: the 5 s listing pass sees the hub's (held, up to 30 s behind)
   * activity move and refetches the latest page at most once per MIN_REFETCH_MS. After: the gateway pushes once
   * a second and the cache takes each delta. Counts fetches, rows transferred and how far the cache trailed.
   */
  async function simulate(push: boolean) {
    const HUB_HOLD_MS = 30_000
    let hubSeen = { activity: 0, at: 0, count: 0 }

    const env = setup({
      records: () => [{ activity: hubSeen.activity, gateway: 'hub', id: 'turn', message_count: hubSeen.count }]
    })

    env.gateway.append('turn', 20)
    hubSeen = { activity: 20, at: env.now(), count: 20 }
    await env.prefetcher.tick()

    if (push) {
      await env.push(env.gateway.append('turn')) // first push: learns the revision (one page)
    }

    env.gateway.latestCalls.length = 0
    env.gateway.afterCalls.length = 0
    env.gateway.rowsServed = 0
    let maxLag = 0
    const start = env.now()

    for (let ms = 500; ms <= 60_000; ms += 500) {
      env.advance(500)
      const moved = env.gateway.append('turn')

      // The hub replica publishes a liveness move at most once per hold window.
      if (env.now() - hubSeen.at >= HUB_HOLD_MS) {
        hubSeen = { activity: moved.activity ?? 0, at: env.now(), count: moved.message_count ?? 0 }
      }

      if (push && ms % 1_000 === 0) {
        await env.push(moved)
      }

      if ((env.now() - start) % 5_000 === 0) {
        await env.prefetcher.tick()
      }

      const served = env.gateway.rows.get('turn')!.length
      const cached = env.cachedIds('turn').length
      maxLag = Math.max(maxLag, served - cached)
    }

    return {
      deltaFetches: env.gateway.afterCalls.length,
      latestPageFetches: env.gateway.latestCalls.length,
      maxRowsBehind: maxLag,
      rowsTransferred: env.gateway.rowsServed
    }
  }

  it('reports fetches before/after and keeps the cache within a second of the turn', async () => {
    const before = await simulate(false)
    const after = await simulate(true)
    console.log(
      `[transcript-push measurement] 60 s turn, 1 row / 500 ms: before=${JSON.stringify(before)} after=${JSON.stringify(after)}`
    )

    expect(after.latestPageFetches).toBe(0)
    expect(after.maxRowsBehind).toBeLessThanOrEqual(2) // at most one push window (1 s = 2 rows)
    expect(before.maxRowsBehind).toBeGreaterThan(after.maxRowsBehind)
    expect(after.rowsTransferred).toBeLessThan(before.rowsTransferred)
  })
})
