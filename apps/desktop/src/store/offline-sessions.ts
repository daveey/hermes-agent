/**
 * [daveey] Cached sessions of gateways that are not reachable right now.
 *
 * The hermes-hub replica (hub-sync plugin) remembers every gateway's session
 * rows. While a gateway is down, those rows become FALLBACK INPUTS to the
 * ordinary sidebar pipeline: they are converted to the same `SessionInfo` the
 * live listing produces and merged into the list the sidebar already filters,
 * groups, pins and sorts. There is no second list and no second predicate —
 * the automated/test toggle, hidden gateways, profile scope, pins, lineage and
 * ordering all apply to a cached row exactly as to a live one.
 *
 * Honesty rules: a cached row is marked (`offline_cached`), never claims a turn
 * is running (`is_active` false), sorts by the session's own activity (never the
 * sync time) and loses to any live row of the same identity.
 */
import { atom } from 'nanostores'

import type { SessionInfo } from '@/hermes'
import { normalizeSessionSource, SIDEBAR_EXCLUDED_SOURCES } from '@/lib/session-source'
import { syncedAgoLabel } from '@/lib/synced-ago'

/** One hub replica session record, as the `hub-sync` plugin serves it. */
export interface OfflineReplicaRecord {
  gateway: string
  id: string
  key: string
  local: boolean
  stale: boolean
  synced_at: null | number
  version: null | number
  activity?: null | number
  archived?: boolean
  /** Owner-stamped (`sessions.automated_sources` of the owning profile). */
  automated?: boolean
  end_reason?: null | string
  ended_at?: null | number
  hidden?: boolean
  message_count?: null | number
  model?: null | string
  parent_session_id?: null | string
  pinned?: boolean
  profile?: string
  running?: boolean
  source?: null | string
  started_at?: null | number
  title?: null | string
}

export { syncedAgoLabel }

/** Cached rows to feed the sidebar; set by the hub-sync plugin's poller. */
export const $offlineSessions = atom<SessionInfo[]>([])

const profileOf = (profile: null | string | undefined) => (profile ?? '').trim() || 'default'
const identity = (connectionId: string, profile: string, id: string) => `${connectionId}\u0000${profile}\u0000${id}`

/** Last authoritative list result per connection/profile. A status probe may
 * invalidate a list, but only a successful list can make retained rows live. */
export const $sessionListFreshness = atom<Record<string, boolean>>({})

const ownerKey = (connection: string | undefined, profile: string | undefined) =>
  `${connection?.trim() || 'local'}\u0000${profileOf(profile)}`

function publishFreshness(next: Record<string, boolean>): void {
  const previous = $sessionListFreshness.get()

  if (
    Object.keys(previous).length !== Object.keys(next).length ||
    Object.keys(next).some(key => previous[key] !== next[key])
  ) {
    $sessionListFreshness.set(next)
  }
}

export function sessionOwnerIsFresh(
  connection: string | undefined,
  profile?: string,
  state = $sessionListFreshness.get()
): boolean {
  return state[ownerKey(connection, profile)] ?? state[ownerKey(connection, '*')] ?? true
}

export function recordSessionGatewayUnavailable(connection: string): void {
  const prefix = `${connection}\u0000`

  const next = Object.fromEntries(
    Object.entries($sessionListFreshness.get()).filter(([key]) => !key.startsWith(prefix))
  )

  next[ownerKey(connection, '*')] = false
  publishFreshness(next)
}

/** Called only by the owning, non-superseded sidebar refresh. */
export function recordSessionListResult(
  previous: readonly SessionInfo[],
  incoming: readonly SessionInfo[],
  connection: string | undefined,
  profile: string,
  errors: readonly { profile?: string; connection_id?: string }[] = [],
  failed = false
): void {
  const next = { ...$sessionListFreshness.get() }
  const owners = new Map<string, { connection_id?: string; profile?: string }>()
  owners.set(ownerKey(connection, profile === 'all' ? '*' : profile), {
    connection_id: connection,
    profile: profile === 'all' ? '*' : profile
  })

  for (const row of [...previous, ...incoming]) {
    if (profile === 'all' || profileOf(row.profile) === profileOf(profile)) {
      owners.set(ownerKey(row.connection_id, row.profile), row)
    }
  }

  for (const error of errors) {
    if (error.profile && error.profile !== 'all') {
      owners.set(ownerKey(error.connection_id ?? connection, error.profile), {
        connection_id: error.connection_id ?? connection,
        profile: error.profile
      })
    }
  }

  const returnedOwners = new Set(incoming.map(row => ownerKey(row.connection_id, row.profile)))

  for (const [key, owner] of owners) {
    const ownerFailed =
      failed ||
      errors.some(
        error =>
          (!error.connection_id || error.connection_id === owner.connection_id) &&
          (error.profile === 'all' || profileOf(error.profile) === profileOf(owner.profile))
      )

    if (returnedOwners.has(key) && !failed) {
      next[key] = true
    } else if (ownerFailed) {
      next[key] = false
    } else if ((owner.connection_id || 'local') === (connection || 'local')) {
      next[key] = true
    }
  }

  publishFreshness(next)
}

/**
 * Replica records -> sidebar rows. Eligibility is the normal recents rule:
 * sources the recents query excludes (cron, subagent, messaging, …), empty
 * chats, archived/hidden rows and untitled rows (no preview is cached, so the
 * only label would be the raw id) stay out; compression chains collapse to
 * their tip. Gateways without a registry connection are skipped because
 * gateway selection / hiding / routing key on the registry connection id.
 */
export function offlineSessionsFromReplica(
  records: readonly OfflineReplicaRecord[],
  connectionForGateway: (gateway: string) => string | undefined
): SessionInfo[] {
  // Read lazily: callers that mock @/lib/session-source never reach the conversion.
  const excluded = new Set<string>(SIDEBAR_EXCLUDED_SOURCES)
  const byId = new Map<string, OfflineReplicaRecord>()

  for (const record of records) {
    if (record?.id) {
      byId.set(recordKey(record, record.id), record)
    }
  }

  const parentOf = (record: OfflineReplicaRecord) =>
    record.parent_session_id ? byId.get(recordKey(record, record.parent_session_id)) : undefined

  const superseded = new Set<OfflineReplicaRecord>()

  for (const record of byId.values()) {
    const parent = parentOf(record)

    if (parent && isCompressionAncestor(parent)) {
      superseded.add(parent)
    }
  }

  const out: SessionInfo[] = []

  for (const record of byId.values()) {
    const connectionId = connectionForGateway(record.gateway)

    if (connectionId && isRecentsEligible(record, excluded) && !superseded.has(record)) {
      out.push(rowFromRecord(record, connectionId, lineageOf(record, parentOf)))
    }
  }

  return out
}

const recordKey = (record: OfflineReplicaRecord, id: string) =>
  `${record.gateway}\u0000${profileOf(record.profile)}\u0000${id}`

const isCompressionAncestor = (record: OfflineReplicaRecord) => record.end_reason === 'compression'

/** The normal recents rule: no excluded source, not empty/archived/hidden, has a title. */
function isRecentsEligible(record: OfflineReplicaRecord, excluded: ReadonlySet<string>): boolean {
  const source = normalizeSessionSource(record.source)

  return !(
    record.local ||
    record.archived ||
    record.hidden ||
    (source && excluded.has(source)) ||
    (record.message_count ?? 0) < 1 ||
    !record.title?.trim()
  )
}

/** Compression ancestors of `record`, oldest first (empty when it has none). */
function lineageOf(
  record: OfflineReplicaRecord,
  parentOf: (r: OfflineReplicaRecord) => OfflineReplicaRecord | undefined
): string[] {
  const lineage: string[] = []
  let ancestor = parentOf(record)

  while (ancestor && isCompressionAncestor(ancestor) && lineage.length < 64) {
    lineage.unshift(ancestor.id)
    ancestor = parentOf(ancestor)
  }

  return lineage
}

function rowFromRecord(record: OfflineReplicaRecord, connectionId: string, lineage: string[]): SessionInfo {
  const startedAt = record.started_at ?? record.activity ?? 0

  return {
    ...(lineage.length ? { _lineage_ids: [...lineage, record.id], _lineage_root_id: lineage[0] } : {}),
    automated: record.automated ?? true,
    connection_id: connectionId,
    ended_at: record.ended_at ?? null,
    id: record.id,
    input_tokens: 0,
    is_active: false,
    last_active: record.activity ?? startedAt,
    message_count: record.message_count ?? 0,
    model: record.model ?? null,
    offline_cached: true,
    offline_gateway: record.gateway,
    offline_synced_at: record.synced_at,
    offline_version: record.version,
    output_tokens: 0,
    pinned: record.pinned === true,
    preview: null,
    profile: profileOf(record.profile),
    source: record.source ?? null,
    started_at: startedAt,
    title: record.title?.trim() ?? '',
    tool_call_count: 0
  }
}

/** Fresh owner lists win; retained rows from a failed list are cached, not live. */
export function mergeOfflineFallback(
  live: SessionInfo[],
  offline: readonly SessionInfo[],
  freshness = $sessionListFreshness.get()
): SessionInfo[] {
  const cachedById = new Map(offline.map(row => [offlineIdentity(row), row]))
  const freshOwners = new Set<string>()
  const seen = new Set<string>()
  let changed = false

  const rows = live.map(row => {
    const fresh = sessionOwnerIsFresh(row.connection_id, row.profile, freshness)
    let effective = row

    if (!fresh) {
      const cached = cachedById.get(offlineIdentity(row))
      effective = cached ?? { ...row, offline_cached: true, is_active: false }
      changed = true
    } else if (!row.offline_cached) {
      freshOwners.add(ownerKey(row.connection_id, row.profile))
    }

    for (const id of [effective.id, effective._lineage_root_id, ...(effective._lineage_ids ?? [])]) {
      if (id) {
        seen.add(identity(effective.connection_id?.trim() ?? '', profileOf(effective.profile), id))
      }
    }

    return effective
  })

  const added = offline.filter(
    row =>
      !freshOwners.has(ownerKey(row.connection_id, row.profile)) &&
      freshness[ownerKey(row.connection_id, row.profile)] !== true &&
      !seen.has(offlineIdentity(row))
  )

  const merged = added.length ? [...rows, ...added] : changed ? rows : live

  return archivedWhileOffline.size
    ? merged.filter(row => !row.offline_cached || !archivedWhileOffline.has(offlineIdentity(row)))
    : merged
}

/** Identity of a cached row for local bookkeeping. */
export const offlineIdentity = (row: Pick<SessionInfo, 'connection_id' | 'id' | 'profile'>): string =>
  identity(row.connection_id?.trim() ?? '', profileOf(row.profile), row.id)

/** The cached row a stored-session id names, if the sidebar is showing one. */
export function findOfflineSession(storedSessionId: string): SessionInfo | undefined {
  return $offlineSessions.get().find(row => row.id === storedSessionId || row._lineage_ids?.includes(storedSessionId))
}

/** Rows the user archived while the owner was offline: hidden until the owner applies the request. */
const archivedWhileOffline = new Set<string>()

export const withoutLocallyArchived = (rows: SessionInfo[]): SessionInfo[] =>
  archivedWhileOffline.size ? rows.filter(row => !archivedWhileOffline.has(offlineIdentity(row))) : rows

export function hideOfflineSession(row: SessionInfo): void {
  archivedWhileOffline.add(offlineIdentity(row))
  $offlineSessions.set(withoutLocallyArchived($offlineSessions.get()))
}

export function _resetOfflineSessionsForTests(): void {
  archivedWhileOffline.clear()
  $sessionListFreshness.set({})
  $offlineSessions.set([])
}
