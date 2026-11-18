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

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { SessionInfo } from '@/hermes'
import { normalizeSessionSource, SIDEBAR_EXCLUDED_SOURCES } from '@/lib/session-source'
import { syncedAgoLabel } from '@/lib/synced-ago'

import { $connectionsRegistry } from './connection-registry-state'

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
  /** Owner-stamped: a delegate_task child (`_delegate_from`), hidden like the local list hides it. */
  is_internal_child?: boolean
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

/** [daveey] desktop.log line per owner whose freshness changed, with what changed it. A stale owner's rows paint as
 *  cached copies (no stripe, no kanban badge), and a 16 min stretch of that after a restart left no trace of which
 *  list failed. Diagnostics only; never throws. */
function logFreshnessFlips(previous: Record<string, boolean>, next: Record<string, boolean>, reason: string): void {
  try {
    const flips = [...new Set([...Object.keys(previous), ...Object.keys(next)])]
      .filter(key => previous[key] !== next[key])
      .map(key => `${key.replace('\u0000', '/')}:${previous[key] ?? '-'}->${next[key] ?? '-'}`)

    if (flips.length && typeof window !== 'undefined') {
      window.hermesDesktop?.logLine?.(`[session-freshness] ${flips.join(' ')} reason=${reason}`)
    }
  } catch {
    // diagnostics only
  }
}

/** [daveey] Owner keys (`connection\0profile`, profile `*` = every profile there) whose live list was fresh at least
 *  once this launch. Never cleared: once an owner has answered, its live rows and the hub-sync replica are newer than
 *  the launch-time boot snapshot, so that snapshot's copies of its chats must not come back when it goes stale. */
export const $answeredSessionOwners = atom<ReadonlySet<string>>(new Set())

function noteAnsweredOwners(next: Record<string, boolean>): void {
  const answered = $answeredSessionOwners.get()
  const fresh = Object.keys(next).filter(key => next[key] === true && !answered.has(key))

  if (fresh.length) {
    $answeredSessionOwners.set(new Set([...answered, ...fresh]))
  }
}

/** True once `connection`/`profile` (or every profile on `connection`) answered a live list this launch. */
export function sessionOwnerAnswered(
  connection: null | string | undefined,
  profile?: null | string,
  answered: ReadonlySet<string> = $answeredSessionOwners.get()
): boolean {
  const conn = connection ?? undefined

  return answered.has(ownerKey(conn, profile ?? undefined)) || answered.has(ownerKey(conn, '*'))
}

function publishFreshness(next: Record<string, boolean>, reason: string): void {
  const previous = $sessionListFreshness.get()
  noteAnsweredOwners(next)

  if (
    Object.keys(previous).length !== Object.keys(next).length ||
    Object.keys(next).some(key => previous[key] !== next[key])
  ) {
    logFreshnessFlips(previous, next, reason)
    $sessionListFreshness.set(next)
  }
}

/** [daveey] Owner keys a sidebar LIST marked stale (failed request, or an error naming them), plus the `<conn>/*` key of
 *  a gateway the hub-sync status probe called unreachable: a re-list with backoff is the only thing that clears it. */
const listStaleOwners = new Set<string>()

/** [daveey] Owners whose last list failed (`connection/profile`) and are still stale. Only a successful list makes
 *  them live again, so the sidebar re-lists while this is non-empty instead of waiting for an unrelated refresh. */
export const staleSessionOwners = (state = $sessionListFreshness.get()): string[] =>
  [...listStaleOwners].filter(key => state[key] === false).map(key => key.replace('\u0000', '/'))

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
  // [daveey] Probe failure invalidates retained rows, but probe success cannot make them authoritative again.
  // Schedule the existing backoff re-list; only a successful list restores freshness.
  listStaleOwners.add(ownerKey(connection, '*'))
  publishFreshness(next, `gateway-unavailable conn=${connection}`)
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
  // [daveey] The primary backend's list can carry other gateways' rows, so its failure is everyone's. A REMOTE
  // backend's list answers only for that gateway: a failed list on the hub (the window's request scope follows the
  // focused chat) used to mark this Mac's own rows stale too, so the sidebar swapped them for stamp-less fallback
  // copies and every local stripe (periodic, waiting, stuck, done) blanked until the next local list. Primary rows
  // carry no connection_id: one identity for both spellings.
  const primary = $connectionsRegistry.get()?.primary?.trim() || 'local'
  const gatewayOf = (id: null | string | undefined) => id?.trim() || primary
  const requestGateway = gatewayOf(connection)

  const covers = (ownerConnection: null | string | undefined, errorConnection?: null | string) =>
    errorConnection?.trim()
      ? gatewayOf(errorConnection) === gatewayOf(ownerConnection)
      : requestGateway === primary || gatewayOf(ownerConnection) === requestGateway

  for (const [key, owner] of owners) {
    const onRequestGateway = gatewayOf(owner.connection_id) === requestGateway

    const ownerFailed =
      (failed && covers(owner.connection_id)) ||
      errors.some(
        error =>
          covers(owner.connection_id, error.connection_id) &&
          (error.profile === 'all' || profileOf(error.profile) === profileOf(owner.profile))
      )

    if (returnedOwners.has(key) && !failed) {
      next[key] = true
    } else if (ownerFailed) {
      next[key] = false
    } else if (onRequestGateway && !failed) {
      next[key] = true
    }
  }

  for (const [key, fresh] of Object.entries(next)) {
    if (fresh) {
      listStaleOwners.delete(key)
    } else if (owners.has(key)) {
      listStaleOwners.add(key)
    }
  }

  publishFreshness(
    next,
    `list conn=${connection?.trim() || 'local'} profile=${profile}${failed ? ' failed' : ''}${errors.length ? ` errors=${errors.length}` : ''}`
  )
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
  connectionForGateway: (gateway: string) => string | undefined,
  nowS: number = Date.now() / 1000,
  // [daveey] The cold-start boot snapshot also shows this gateway's own (`local`) rows until its live list arrives.
  { includeLocal = false }: { includeLocal?: boolean } = {}
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

    if (connectionId && isRecentsEligible(record, excluded, includeLocal) && !superseded.has(record)) {
      out.push(rowFromRecord(record, connectionId, lineageOf(record, parentOf), nowS))
    }
  }

  return out
}

const recordKey = (record: OfflineReplicaRecord, id: string) =>
  `${record.gateway}\u0000${profileOf(record.profile)}\u0000${id}`

const isCompressionAncestor = (record: OfflineReplicaRecord) => record.end_reason === 'compression'

/** The normal recents rule: no excluded source or delegate child, not empty/archived/hidden, has a title. */
function isRecentsEligible(record: OfflineReplicaRecord, excluded: ReadonlySet<string>, includeLocal: boolean): boolean {
  const source = normalizeSessionSource(record.source)

  return !(
    (record.local && !includeLocal) ||
    record.archived ||
    record.hidden ||
    record.is_internal_child === true ||
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

/**
 * [daveey] A replica owner that published within this window is awake: its rows are ordinary sidebar rows (they open
 * the normal way and show its running turns; `hermes-hub watch` keeps them within seconds of the owner). Older than
 * this, the owner is asleep or unreachable and its rows are cached (`offline_cached`): marked, never running.
 */
export const OWNER_ASLEEP_AFTER_S = 180

export function ownerAsleep(record: Pick<OfflineReplicaRecord, 'stale' | 'synced_at'>, nowS: number): boolean {
  return record.stale || record.synced_at == null || nowS - record.synced_at > OWNER_ASLEEP_AFTER_S
}

function rowFromRecord(record: OfflineReplicaRecord, connectionId: string, lineage: string[], nowS: number): SessionInfo {
  const startedAt = record.started_at ?? record.activity ?? 0
  const asleep = ownerAsleep(record, nowS)

  return {
    ...(lineage.length ? { _lineage_ids: [...lineage, record.id], _lineage_root_id: lineage[0] } : {}),
    automated: record.automated ?? true,
    connection_id: connectionId,
    ended_at: record.ended_at ?? null,
    id: record.id,
    input_tokens: 0,
    is_active: !asleep && record.running === true,
    last_active: record.activity ?? startedAt,
    message_count: record.message_count ?? 0,
    model: record.model ?? null,
    offline_cached: asleep,
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

/** [daveey] Every session row a projects tree carries (overview previews and hydrated lanes). */
export function projectTreeSessionRows(nodes: readonly SidebarProjectTree[]): SessionInfo[] {
  return nodes.flatMap(node => [
    ...(node.previewSessions ?? []),
    ...(node.repos ?? []).flatMap(repo => (repo.groups ?? []).flatMap(group => group.sessions ?? []))
  ])
}

/** Fresh owner lists win; retained rows from a failed list are cached, not live. A retained row with no replica
 * copy is stamped `offline_stale_list`: its owner failed one list, which does not prove it is unreachable. Nor does
 * a replica copy of this Mac's own backend (the boot snapshot's, judged asleep at boot): its row is stamped too.
 *
 * [daveey] `treeRows`: rows the backends' projects trees reported. A replica row carries no activity stamps
 * (kanban_*, outcome, goal_active, ...), and the sidebar overlays fallback rows over the tree's own copies ("live
 * wins"), so an awake owner's replica row used to blank the waiting/stuck stripe its backend row paints. When the
 * owner's backend reported that session in its tree, that row is used instead of the replica copy; otherwise the
 * owner's stamps for it (`stamps`, fetched by owner-row-stamps.ts) are merged into the replica copy. */
export function mergeOfflineFallback(
  live: SessionInfo[],
  offline: readonly SessionInfo[],
  freshness = $sessionListFreshness.get(),
  treeRows: readonly SessionInfo[] = [],
  // [daveey] Owner-reported stamps for awake replica rows, keyed like `ownedIdentity` (see owner-row-stamps.ts).
  stamps: ReadonlyMap<string, Partial<SessionInfo>> = new Map()
): SessionInfo[] {
  // [daveey] Primary-backend rows carry no connection_id while replica rows name the primary: one identity for both.
  const primary = $connectionsRegistry.get()?.primary?.trim() || 'local'

  const ownedIdentity = (row: Pick<SessionInfo, 'connection_id' | 'profile'>, id: string) =>
    identity(row.connection_id?.trim() || primary, profileOf(row.profile), id)

  const cachedById = new Map(offline.map(row => [ownedIdentity(row, row.id), row]))
  const freshOwners = new Set<string>()
  const seen = new Set<string>()
  let changed = false

  const rows = live.map(row => {
    const fresh = sessionOwnerIsFresh(row.connection_id, row.profile, freshness)
    let effective = row

    if (!fresh) {
      const ownBackend = (row.connection_id?.trim() || primary) === primary
      const cached = ownBackend ? undefined : cachedById.get(ownedIdentity(row, row.id))
      effective = cached ?? { ...row, is_active: false, offline_cached: true, offline_stale_list: true }
      changed = true
    } else if (!row.offline_cached) {
      freshOwners.add(ownerKey(row.connection_id, row.profile))
    }

    for (const id of [effective.id, effective._lineage_root_id, ...(effective._lineage_ids ?? [])]) {
      if (id) {
        seen.add(ownedIdentity(effective, id))
      }
    }

    return effective
  })

  // [daveey] This Mac's own fallback rows are the cold-start boot snapshot: frozen at launch and without activity
  // stamps (cron_active, kanban_*, goal_active, ...). Once the own backend has put any live row in the list it has
  // answered this launch, and its older chats paint from the live (stamped) projects tree instead; re-adding the boot
  // copies after a failed list blanked their stripes.
  const ownBackendAnswered = live.some(
    row => !row.offline_cached && (row.connection_id?.trim() || primary) === primary
  )

  // [daveey] Backend tree rows by owner identity; replica-derived tree rows (offline_synced_at) are no better.
  const reported = new Map<string, SessionInfo>()

  for (const row of treeRows) {
    if (row?.id && row.offline_synced_at === undefined && !row.offline_cached) {
      reported.set(ownedIdentity(row, row.id), row)
    }
  }

  const added = offline
    .filter(
      row =>
        !(ownBackendAnswered && (row.connection_id?.trim() || primary) === primary) &&
        !freshOwners.has(ownerKey(row.connection_id, row.profile)) &&
        freshness[ownerKey(row.connection_id, row.profile)] !== true &&
        !seen.has(ownedIdentity(row, row.id))
    )
    // An asleep owner's row keeps its cached (read-only) marking; an awake one's backend row wins. Outside the tree,
    // the owner's own stamps (owner-row-stamps.ts) are folded into the replica row.
    .map(row => {
      if (row.offline_cached) {
        return row
      }

      const key = ownedIdentity(row, row.id)
      const own = stamps.get(key)

      return reported.get(key) ?? (own ? { ...row, ...own } : row)
    })

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
  listStaleOwners.clear()
  $answeredSessionOwners.set(new Set())
  $sessionListFreshness.set({})
  $offlineSessions.set([])
}
