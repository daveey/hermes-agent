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

export interface PrefetchStore {
  meta(key: TranscriptCacheKey): { activity: null | number; messageCount: null | number; savedAt: number } | null
  putLatest(key: TranscriptCacheKey, latest: readonly unknown[], meta?: TranscriptCacheMeta): boolean
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
    if (!record?.id || !record.gateway || record.archived || record.hidden) {
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
export function needsRefresh(
  cached: { activity: null | number; messageCount: null | number; savedAt: number } | null,
  target: PrefetchTarget,
  now: number
): boolean {
  if (!cached) {
    return true
  }

  const moved = cached.activity !== target.meta.activity || cached.messageCount !== target.meta.messageCount

  return moved && now - cached.savedAt >= MIN_REFETCH_MS
}

export class TranscriptPrefetcher {
  private readonly deps: PrefetchDeps
  private readonly downUntil = new Map<string, number>()
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

    const due = targets.filter(target => needsRefresh(this.deps.store.meta(target.key), target, now))
    result.skipped = targets.length - due.length

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

              if (rows.length > 0 && this.deps.store.putLatest(target.key, rows, target.meta)) {
                result.fetched += 1
              } else {
                result.skipped += 1
              }
            } catch (error) {
              result.failed += 1

              if (isGatewayFailure(error)) {
                this.downUntil.set(connectionId, (this.deps.now?.() ?? Date.now()) + GATEWAY_BACKOFF_MS)
                this.deps.log?.(
                  `transcript prefetch: ${connectionId} unreachable (${(error as Error)?.message || error}); backing off`
                )
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
