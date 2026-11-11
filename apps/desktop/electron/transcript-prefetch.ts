/**
 * [daveey] Background transcript prefetch (instant session loads, C1).
 *
 * The local hermes-hub replica (`GET /api/plugins/hub-sync/sessions`, fresh within
 * about a second via `hermes-hub watch`) lists every gateway's sessions with their
 * `activity` and `message_count`. Each tick picks, per gateway, the PER_GATEWAY most
 * recently active session tips and fetches the latest page of any whose cached copy
 * was taken against a different activity/message_count. So a click on any of them
 * paints from the local cache, and a session that moved is refetched on the next
 * tick. All I/O is injected; electron/main.ts wires it to ensureRegistryBackend.
 */
import { LATEST_ROWS, type TranscriptCacheKey, type TranscriptCacheMeta } from './transcript-cache'

export const PER_GATEWAY = 200
/** A session whose activity keeps moving (a running turn) is refetched at most this often. */
export const MIN_REFETCH_MS = 30_000
/** A gateway that failed is skipped for this long before it is tried again. */
export const GATEWAY_BACKOFF_MS = 60_000
/** [daveey] On-demand (`@session` ref) fetches in flight per gateway (on top of the tick's two), and the most
 *  queued behind them. */
export const DEMAND_CONCURRENCY = 1
export const DEMAND_QUEUE_MAX = 50
/** [daveey] A cached session the hub listing does not cover is refetched on demand at most this often. */
export const UNLISTED_REFETCH_MS = 10 * 60_000
const DEMAND_MISSED_MAX = 500

export interface HubSessionRecord {
  activity?: null | number
  archived?: boolean
  automated?: boolean
  gateway: string
  hidden?: boolean
  id: string
  /** [daveey] An open Inbox card names this session: it is the one a click is about to open. */
  inbox?: boolean
  message_count?: null | number
  parent_session_id?: null | string
  profile?: null | string
  started_at?: null | number
}

export interface PrefetchTarget {
  automated: boolean
  inbox?: boolean
  key: TranscriptCacheKey
  meta: { activity: null | number; messageCount: null | number }
}

type CachedMeta = { activity: null | number; messageCount: null | number; savedAt: number } | null
type MaybePromise<T> = Promise<T> | T

/**
 * The cache as the prefetcher sees it. Every call may be asynchronous: Desktop runs the SQLite store on a worker
 * thread (transcript-cache-client.ts), because a write that waits on the disk froze Electron's main loop for
 * 1-1.8 s. `metaMany` answers a whole pass's freshness checks in one round trip.
 */
export interface PrefetchStore {
  meta(key: TranscriptCacheKey): MaybePromise<CachedMeta>
  metaMany?(keys: readonly TranscriptCacheKey[]): MaybePromise<CachedMeta[]>
  putLatest(key: TranscriptCacheKey, latest: readonly unknown[], meta?: TranscriptCacheMeta): MaybePromise<boolean>
}

export interface PrefetchDeps {
  /** Registry connection id of a hub gateway name, or undefined (unregistered gateway). */
  connectionForGateway: (gateway: string) => Promise<string | undefined> | string | undefined
  /** Latest `limit` raw display rows of one session (chronological). */
  fetchLatest: (key: TranscriptCacheKey, limit: number) => Promise<unknown[]>
  listHubSessions: () => Promise<HubSessionRecord[] | null>
  log?: (line: string) => void
  now?: () => number
  store: PrefetchStore
}

const profileOf = (value: unknown) => String(value ?? '').trim() || 'default'
const keyString = (key: TranscriptCacheKey) => `${key.connectionId}\u0000${key.profile}\u0000${key.sessionId}`

/** What one on-demand prefetch did: fetched/empty/failed ran a fetch; the rest skipped it. */
export type PrefetchKeyOutcome =
  'backoff' | 'current' | 'dropped' | 'empty' | 'failed' | 'fetched' | 'invalid' | 'queued'

/**
 * Per gateway, the PER_GATEWAY most recently active session tips: open Inbox cards' sessions first, then people's
 * chats before automations. A record that is another record's compression parent is
 * history of that tip, not its own session. Fetch order: chats first, newest first.
 */
export function selectPrefetchTargets(
  records: readonly HubSessionRecord[],
  connectionForGateway: (gateway: string) => string | undefined,
  perGateway: number = PER_GATEWAY
): PrefetchTarget[] {
  const parents = new Set(
    records
      .filter(record => record.parent_session_id)
      .map(record => `${record.gateway}\u0000${profileOf(record.profile)}\u0000${record.parent_session_id}`)
  )

  const byGateway = new Map<string, HubSessionRecord[]>()

  for (const record of records) {
    // An open Inbox card's session is prefetched even when hidden or archived (cards usually own such sessions).
    if (!record?.id || !record.gateway || ((record.archived || record.hidden) && !record.inbox)) {
      continue
    }

    if (parents.has(`${record.gateway}\u0000${profileOf(record.profile)}\u0000${record.id}`)) {
      continue
    }

    byGateway.set(record.gateway, [...(byGateway.get(record.gateway) ?? []), record])
  }

  const activityOf = (record: HubSessionRecord) => Number(record.activity ?? record.started_at ?? 0)
  const targets: PrefetchTarget[] = []

  for (const [gateway, list] of byGateway) {
    const connectionId = connectionForGateway(gateway)

    if (!connectionId) {
      continue
    }

    // People's chats claim the slots first: a gateway running many automations
    // (Kanban, cron) would otherwise push the chats someone opens out of the window.
    // [daveey] Sessions behind open Inbox cards come first: they are mostly automations (self-heal, Kanban), so a
    // gateway with many chats otherwise never gave them a slot, and an Inbox click always waited on the network.
    const byActivity = [...list].sort((a, b) => activityOf(b) - activityOf(a))

    const chosen = [
      ...byActivity.filter(record => record.inbox),
      ...byActivity.filter(record => !record.inbox && !record.automated),
      ...byActivity.filter(record => !record.inbox && record.automated)
    ]

    for (const record of chosen.slice(0, perGateway)) {
      targets.push({
        automated: Boolean(record.automated),
        ...(record.inbox ? { inbox: true } : {}),
        key: { connectionId, profile: profileOf(record.profile), sessionId: record.id },
        meta: { activity: record.activity ?? null, messageCount: record.message_count ?? null }
      })
    }
  }

  return targets.sort(
    (a, b) =>
      Number(Boolean(b.inbox)) - Number(Boolean(a.inbox)) ||
      Number(a.automated) - Number(b.automated) ||
      (b.meta.activity ?? 0) - (a.meta.activity ?? 0)
  )
}

/** Whether a cached entry is behind the hub's view of the session. */
export function needsRefresh(cached: CachedMeta, target: PrefetchTarget, now: number): boolean {
  if (!cached) {
    return true
  }

  const moved = cached.activity !== target.meta.activity || cached.messageCount !== target.meta.messageCount

  return moved && now - cached.savedAt >= MIN_REFETCH_MS
}

export class TranscriptPrefetcher {
  private readonly deps: PrefetchDeps
  private readonly downUntil = new Map<string, number>()
  /**
   * Targets whose last fetch stored nothing (empty page, 404, unstorable): the hub meta they were tried at.
   * The cache has no row for them, so without this needsRefresh() saw "never fetched" and every tick refetched
   * them. They are retried once their activity/message_count moves (and MIN_REFETCH_MS passed).
   */
  private readonly unstored = new Map<
    string,
    { activity: null | number; messageCount: null | number; savedAt: number }
  >()
  /** [daveey] Hub meta of every session the last pass listed, so an on-demand fetch can tell a current cache. */
  private readonly listed = new Map<string, { activity: null | number; messageCount: null | number }>()
  /** [daveey] On-demand fetches: queued/in-flight keys (with their waiters), per-gateway queues, active counts. */
  private readonly demandWaiters = new Map<string, ((outcome: PrefetchKeyOutcome) => void)[]>()
  private readonly demandQueues = new Map<string, TranscriptCacheKey[]>()
  private readonly demandActive = new Map<string, number>()
  /** Keys whose freshness check is in flight (the store answers asynchronously): later asks share its answer. */
  private readonly demandChecks = new Map<string, Promise<boolean>>()
  /** [daveey] On-demand keys whose fetch stored nothing (404, empty): not retried within MIN_REFETCH_MS. */
  private readonly demandMissed = new Map<
    string,
    { activity: null | number; messageCount: null | number; savedAt: number }
  >()
  private running: null | Promise<PrefetchTickResult> = null
  private timer: null | ReturnType<typeof setTimeout> = null
  private stopped = true

  constructor(deps: PrefetchDeps) {
    this.deps = deps
  }

  /** One pass; concurrent calls share the pass in flight. */
  tick(): Promise<PrefetchTickResult> {
    if (!this.running) {
      this.running = this.pass().finally(() => {
        this.running = null
      })
    }

    return this.running
  }

  /**
   * [daveey] Warm one session the renderer is about to open (an `@session` ref chip rendered or was hovered).
   * Such refs mostly point at old sessions outside the PER_GATEWAY window, so without this the first click waited
   * on REST. Fire-and-forget for the caller; the promise settles when this key is done (tests, logs). Deduped
   * against queued/in-flight keys; skipped while the cache is current (the hub's activity/message_count when the
   * last pass listed the session, else a copy younger than UNLISTED_REFETCH_MS) or the gateway is backed off; at most
   * DEMAND_CONCURRENCY fetches per gateway at a time and DEMAND_QUEUE_MAX queued behind them.
   */
  prefetchKey(raw: TranscriptCacheKey): Promise<PrefetchKeyOutcome> {
    return this.enqueueDemand(raw).catch(error => {
      // A store read can fail (closed at quit, SQLITE_BUSY, worker gone): never out of an IPC listener.
      this.deps.log?.(`transcript prefetch (ref) skipped: ${(error as Error)?.message || error}`)

      return 'failed' as const
    })
  }

  private async enqueueDemand(raw: TranscriptCacheKey): Promise<PrefetchKeyOutcome> {
    const key: TranscriptCacheKey = {
      connectionId: String(raw?.connectionId ?? '').trim(),
      profile: profileOf(raw?.profile),
      sessionId: String(raw?.sessionId ?? '').trim()
    }

    const id = keyString(key)
    const now = this.deps.now?.() ?? Date.now()

    if (!key.connectionId || !key.sessionId) {
      return 'invalid'
    }

    if (this.demandWaiters.has(id)) {
      return 'queued'
    }

    if ((this.downUntil.get(key.connectionId) ?? 0) > now) {
      return 'backoff'
    }

    const checking = this.demandChecks.get(id)

    if (checking) {
      return (await checking) ? 'queued' : 'current'
    }

    const check = Promise.resolve()
      .then(() => this.demandDue(key, now))
      .finally(() => this.demandChecks.delete(id))

    this.demandChecks.set(id, check)

    if (!(await check)) {
      return 'current'
    }

    const queue = this.demandQueues.get(key.connectionId) ?? []

    if (queue.length >= DEMAND_QUEUE_MAX) {
      return 'dropped'
    }

    return new Promise<PrefetchKeyOutcome>(resolve => {
      this.demandWaiters.set(id, [resolve])
      queue.push(key)
      this.demandQueues.set(key.connectionId, queue)
      this.pumpDemand(key.connectionId)
    })
  }

  private async demandDue(key: TranscriptCacheKey, now: number): Promise<boolean> {
    const id = keyString(key)
    const cached = (await this.deps.store.meta(key)) ?? this.unstored.get(id) ?? this.demandMissed.get(id) ?? null
    const listed = this.listed.get(id)

    if (!cached) {
      return true
    }

    if (listed) {
      return needsRefresh(cached, { automated: false, key, meta: listed }, now)
    }

    // Not in the hub listing (past its limit, or hub-sync unreadable): nothing says it moved. Refs mostly name
    // such old sessions, and the open's own REST read refreshes the cache, so only a long-stale copy is refetched.
    return now - cached.savedAt >= UNLISTED_REFETCH_MS
  }

  private pumpDemand(connectionId: string): void {
    const queue = this.demandQueues.get(connectionId) ?? []

    while (queue.length > 0 && (this.demandActive.get(connectionId) ?? 0) < DEMAND_CONCURRENCY) {
      const key = queue.shift()!
      this.demandActive.set(connectionId, (this.demandActive.get(connectionId) ?? 0) + 1)

      void this.fetchDemand(key).then(outcome => {
        this.demandActive.set(connectionId, Math.max(0, (this.demandActive.get(connectionId) ?? 1) - 1))
        const waiters = this.demandWaiters.get(keyString(key)) ?? []
        this.demandWaiters.delete(keyString(key))
        waiters.forEach(resolve => resolve(outcome))
        this.pumpDemand(connectionId)
      })
    }

    if (queue.length === 0) {
      this.demandQueues.delete(connectionId)
    }
  }

  private async fetchDemand(key: TranscriptCacheKey): Promise<PrefetchKeyOutcome> {
    const id = keyString(key)
    const meta = this.listed.get(id) ?? { activity: null, messageCount: null }
    const stamp = () => this.deps.now?.() ?? Date.now()

    const markMissed = () => {
      this.demandMissed.delete(id)
      this.demandMissed.set(id, { ...meta, savedAt: stamp() })

      // Bounded: drop the oldest misses past the cap (Map keeps insertion order).
      for (const stale of this.demandMissed.keys()) {
        if (this.demandMissed.size <= DEMAND_MISSED_MAX) {
          break
        }

        this.demandMissed.delete(stale)
      }
    }

    if ((this.downUntil.get(key.connectionId) ?? 0) > stamp()) {
      return 'backoff'
    }

    try {
      const rows = await this.deps.fetchLatest(key, LATEST_ROWS)

      if (rows.length > 0 && (await this.deps.store.putLatest(key, rows, meta))) {
        this.demandMissed.delete(id)
        this.deps.log?.(`transcript prefetch (ref): cached ${key.connectionId}/${key.profile}/${key.sessionId}`)

        return 'fetched'
      }

      markMissed()

      return 'empty'
    } catch (error) {
      if (isGatewayFailure(error)) {
        this.downUntil.set(key.connectionId, stamp() + GATEWAY_BACKOFF_MS)
        this.deps.log?.(
          `transcript prefetch (ref): ${key.connectionId} unreachable (${(error as Error)?.message || error}); backing off`
        )
      } else {
        markMissed() // a 4xx for this one session
      }

      return 'failed'
    }
  }

  start(intervalMs = 5_000): void {
    if (!this.stopped) {
      return
    }

    this.stopped = false

    const loop = async () => {
      try {
        await this.tick()
      } catch (error) {
        this.deps.log?.(`transcript prefetch tick failed: ${(error as Error)?.message || error}`)
      }

      if (!this.stopped) {
        this.timer = setTimeout(loop, intervalMs)
        this.timer.unref?.()
      }
    }

    void loop()
  }

  stop(): void {
    this.stopped = true

    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private async pass(): Promise<PrefetchTickResult> {
    const now = this.deps.now?.() ?? Date.now()
    const result: PrefetchTickResult = { failed: 0, fetched: 0, skipped: 0, targets: 0 }
    const records = await this.deps.listHubSessions()

    if (!records) {
      this.listed.clear() // stale hub meta must not keep a moved session reading as current

      return result
    }

    const names = new Map<string, string | undefined>()

    for (const gateway of new Set(records.map(record => record.gateway))) {
      try {
        names.set(gateway, await this.deps.connectionForGateway(gateway))
      } catch {
        names.set(gateway, undefined)
      }
    }

    const targets = selectPrefetchTargets(records, gateway => names.get(gateway))
    result.targets = targets.length

    // [daveey] Every listed session's hub meta (not just the targets), for on-demand freshness checks.
    this.listed.clear()

    for (const record of records) {
      const connectionId = record?.id && record.gateway ? names.get(record.gateway) : undefined

      if (connectionId) {
        this.listed.set(keyString({ connectionId, profile: profileOf(record.profile), sessionId: record.id }), {
          activity: record.activity ?? null,
          messageCount: record.message_count ?? null
        })
      }
    }

    const keyOf = (target: PrefetchTarget) => keyString(target.key)

    // Forget memo entries for sessions no longer targeted, so the map stays bounded by the target set.
    const live = new Set(targets.map(keyOf))

    for (const key of this.unstored.keys()) {
      if (!live.has(key)) {
        this.unstored.delete(key)
      }
    }

    const keys = targets.map(target => target.key)
    const { store } = this.deps
    const cached = store.metaMany ? await store.metaMany(keys) : await Promise.all(keys.map(key => store.meta(key)))

    const due = targets.filter((target, index) =>
      needsRefresh(cached[index] ?? this.unstored.get(keyOf(target)) ?? null, target, now)
    )

    result.skipped = targets.length - due.length

    const markUnstored = (target: PrefetchTarget) =>
      this.unstored.set(keyOf(target), { ...target.meta, savedAt: this.deps.now?.() ?? Date.now() })

    // Two fetches per gateway at a time: one slow SSH route never stalls the rest.
    const queues = new Map<string, PrefetchTarget[]>()

    for (const target of due) {
      queues.set(target.key.connectionId, [...(queues.get(target.key.connectionId) ?? []), target])
    }

    await Promise.all(
      [...queues].map(async ([connectionId, queue]) => {
        const worker = async () => {
          while (queue.length > 0) {
            if ((this.downUntil.get(connectionId) ?? 0) > (this.deps.now?.() ?? Date.now())) {
              result.skipped += queue.length
              queue.length = 0

              return
            }

            const target = queue.shift()!

            try {
              const rows = await this.deps.fetchLatest(target.key, LATEST_ROWS)

              if (rows.length > 0 && (await this.deps.store.putLatest(target.key, rows, target.meta))) {
                this.unstored.delete(keyOf(target))
                result.fetched += 1
              } else {
                markUnstored(target)
                result.skipped += 1
              }
            } catch (error) {
              result.failed += 1

              if (isGatewayFailure(error)) {
                this.downUntil.set(connectionId, (this.deps.now?.() ?? Date.now()) + GATEWAY_BACKOFF_MS)
                this.deps.log?.(
                  `transcript prefetch: ${connectionId} unreachable (${(error as Error)?.message || error}); backing off`
                )
              } else {
                markUnstored(target) // a 4xx for this one session: wait for it to move
              }
            }
          }
        }

        await Promise.all([worker(), worker()])
      })
    )

    if (result.fetched || result.failed) {
      this.deps.log?.(
        `transcript prefetch: ${result.fetched} fetched, ${result.failed} failed, ${result.skipped} current of ${result.targets}`
      )
    }

    return result
  }
}

export interface PrefetchTickResult {
  failed: number
  fetched: number
  skipped: number
  targets: number
}

/** A 404 is one missing session; anything without an HTTP status is the route itself. */
function isGatewayFailure(error: unknown): boolean {
  const status = Number((error as { status?: unknown; statusCode?: unknown })?.status ?? (error as any)?.statusCode)

  return !(status >= 400 && status < 500)
}
