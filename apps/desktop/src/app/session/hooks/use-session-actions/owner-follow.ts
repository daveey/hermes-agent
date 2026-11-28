import type { JsonRpcGatewayError } from '@hermes/shared'

import { getSession } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { hubSessionOwner } from '@/store/hub-sync'
import {
  $connection,
  forgetSessionOwnerHintsForSession,
  getSessionOwnerHints,
  ownerLookupSessionRows,
  sessionMatchesStoredId,
  setSessionOwnerHint
} from '@/store/session'
import {
  isSessionOwnerRoute,
  type SessionOwnerFollower,
  type SessionOwnerRoute,
  sessionOwnerRouteFromRow,
  type SessionOwnerScope,
  setSessionOwnerFollower
} from '@/store/session-request-router'
import { $sessionTiles, patchSessionTile, storedSessionIdForRuntimeId } from '@/store/session-states'

import { cachedSessionRowCandidates } from './utils'

/**
 * Following a session to the gateway that owns it [daveey].
 *
 * The sidebar unifies every connected gateway's sessions, so a stored id can be
 * opened while the window has a DIFFERENT gateway selected. A resume dispatched
 * on that ambient/stale route lands on a backend that never minted the id: it
 * answers JSON-RPC 4007 ("session not found"). That answer is proof the route
 * was wrong, not that the session is gone — so before the not-found handling
 * runs, find the connection that does own the id and resume there.
 */

/** JSON-RPC 4007 — the gateway has no stored session by that id. */
export const SESSION_NOT_FOUND_CODE = 4007

/** JSON-RPC 4001 — the gateway holds no live runtime by that id ("not in memory"). */
export const SESSION_NOT_IN_MEMORY_CODE = 4001

/** JSON-RPC 4132 — the gateway refuses to resume a Kanban worker transcript
 *  (tui_gateway _resume_guard): a resumed chat would be a second, unsupervised
 *  writer next to the dispatcher-owned run. The stored transcript is still
 *  readable over REST, so Desktop opens it read-only instead of erroring. */
export const KANBAN_WORKER_RESUME_REFUSED_CODE = 4132

export function isKanbanWorkerResumeRefusal(error: unknown): boolean {
  return (
    Boolean(error) &&
    typeof error === 'object' &&
    (error as Partial<JsonRpcGatewayError>).code === KANBAN_WORKER_RESUME_REFUSED_CODE
  )
}

/** True for a gateway's "no such stored session" answer to `session.resume`.
 *  A bare 4001 (live runtime reaped) is a different signal and does not
 *  qualify: the stored session exists there, only its runtime is gone. */
export function isStoredSessionNotFound(error: unknown): boolean {
  if (!error || typeof error !== 'object') {
    return false
  }

  const code = (error as Partial<JsonRpcGatewayError>).code

  if (typeof code === 'number') {
    return code === SESSION_NOT_FOUND_CODE
  }

  return error instanceof Error && /\bsession not found\b/i.test(error.message)
}

/** The registry connection a session-scoped RPC with `owner` was dispatched
 *  on. An exact route names it; anything else rode the selected (ambient)
 *  connection, else the window primary. */
export function dispatchedConnectionId(owner: SessionOwnerScope): string {
  if (isSessionOwnerRoute(owner)) {
    return owner.connectionId.trim()
  }

  return $connection.get()?.connectionId?.trim() || $connectionsRegistry.get()?.primary?.trim() || 'local'
}

function registryConnectionIds(): string[] {
  return ($connectionsRegistry.get()?.connections ?? []).map(connection => connection.id?.trim() ?? '').filter(Boolean)
}

/**
 * The owner of `storedSessionId` on a connection OTHER than `excluded`, from
 * the unified session list (every connection's connection-tagged rows), then
 * the persisted owner hints, then the hermes-hub replica's session → gateway
 * map (one local read on the primary gateway, [daveey]), then a read-only by-id
 * probe of each remaining registry connection. Undefined when no other connection has it — or when
 * there is no other connection at all (single-gateway installs never probe).
 */
export async function findForeignSessionOwner(
  storedSessionId: string,
  excluded: readonly string[],
  { probe = true }: { probe?: boolean } = {}
): Promise<SessionOwnerRoute | undefined> {
  const id = storedSessionId.trim()
  const skip = new Set(excluded.map(connection => connection.trim()).filter(Boolean))

  if (!id) {
    return undefined
  }

  const rows = [
    ...cachedSessionRowCandidates(id),
    ...ownerLookupSessionRows().filter(row => sessionMatchesStoredId(row, id))
  ]

  for (const row of rows) {
    const route = sessionOwnerRouteFromRow(row)

    if (route && !skip.has(route.connectionId)) {
      return route
    }
  }

  const hinted = getSessionOwnerHints(id).find(route => !skip.has(route.connectionId.trim()))

  if (hinted) {
    return hinted
  }

  // [daveey] The hub replica knows which gateway owns every synced session,
  // with the owner asleep or the hub down; the probe below is the fallback for
  // ids the replica hasn't seen. Cheap enough for the 4001 path too (one cached
  // local read), and it can only name a connection outside `skip`.
  if (registryConnectionIds().length >= 2) {
    const viaHub = await hubSessionOwner(id, skip)

    if (viaHub) {
      return viaHub
    }
  }

  const candidates = registryConnectionIds().filter(connectionId => !skip.has(connectionId))

  if (!probe || candidates.length === 0 || registryConnectionIds().length < 2) {
    return undefined
  }

  for (const connectionId of candidates) {
    try {
      const row = await getSession(id, { connectionId, profile: 'default' })

      return { connectionId, profile: row?.profile?.trim() || 'default' }
    } catch {
      // Not on this connection (or it is unreachable); try the next.
    }
  }

  return undefined
}

/** Record `route` as the session's ONLY owner: a 4007 just proved every other
 *  remembered route wrong, and two hints for one id make the hint ambiguous
 *  (getSessionOwnerHint answers undefined), which re-opens the ambient path. */
export function pinSessionOwner(storedSessionId: string, route: SessionOwnerRoute): void {
  forgetSessionOwnerHintsForSession(storedSessionId)
  setSessionOwnerHint(storedSessionId, route)
}

/** Pin `route` as the session's owner on its open tile too: a tile's saved
 *  route outranks the hint in every owner ladder, so a stale one would keep
 *  sending the session's RPCs (and the next tile resume) to the wrong gateway. */
export function repointSessionTileOwner(storedSessionId: string, route: SessionOwnerRoute): void {
  const tile = $sessionTiles.get().find(candidate => candidate.storedSessionId === storedSessionId)

  if (tile?.ownerRoute && tile.ownerRoute.connectionId.trim() !== route.connectionId.trim()) {
    patchSessionTile(storedSessionId, { ownerRoute: route })
  }
}

// ── Session-scoped RPC follower ─────────────────────────────────────────────
// Registered with the shared session-RPC router (requestForSessionProfile), so
// every session-scoped caller — the window dispatcher, the store pollers
// (process.list, session.control.read, approval.*), tile actions — routes by
// the owner and recovers from a misroute the same way. Inert with fewer than
// two registry connections, so single-gateway installs behave as before.

/** How long a session/gateway pair with no other owner is left alone after a
 *  lookup missed (or the retry failed), so a 5s poller against a session that
 *  really is gone costs one request per tick and no probes. */
const NO_FOREIGN_OWNER_TTL_MS = 60_000
const FOLLOW_MEMO_LIMIT = 256

/** Proven corrections: session id (param or stored) → the gateway that
 *  answered not-found and the owner that answered instead. */
const followedOwners = new Map<string, { from: string; to: SessionOwnerRoute }>()
const noForeignOwnerUntil = new Map<string, number>()
const ownerLookups = new Map<string, Promise<SessionOwnerRoute | undefined>>()

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function rememberBounded<V>(map: Map<string, V>, key: string, value: V): void {
  map.delete(key)
  map.set(key, value)

  if (map.size > FOLLOW_MEMO_LIMIT) {
    const oldest = map.keys().next().value

    if (oldest !== undefined) {
      map.delete(oldest)
    }
  }
}

function multiGateway(): boolean {
  return registryConnectionIds().length >= 2
}

/** The connection an RPC with `owner` really goes to: an exact route names it;
 *  a bare profile takes the primary's profile door; no owner rides ambient. */
export function routedConnectionId(owner: SessionOwnerScope): string {
  if (isSessionOwnerRoute(owner)) {
    return owner.connectionId.trim()
  }

  if (owner && String(owner).trim()) {
    return $connectionsRegistry.get()?.primary?.trim() || 'local'
  }

  return dispatchedConnectionId(owner)
}

/** Runtime → stored ids a caller already translated through ladders the
 *  store cannot see (the window dispatcher's wiring-cache bindings). */
const translatedRuntimeIds = new Map<string, string>()

/** Record the stored id a session-scoped RPC's runtime id belongs to, so a
 *  misroute can be followed by the durable id the owner records key on. */
export function noteRpcStoredSessionId(runtimeId: null | string | undefined, storedSessionId: null | string): void {
  const runtime = runtimeId?.trim()
  const stored = storedSessionId?.trim()

  if (runtime && stored && runtime !== stored) {
    rememberBounded(translatedRuntimeIds, runtime, stored)
  }
}

function rpcSessionIds(params: Record<string, unknown>): { paramId: string; storedId: string } {
  const paramId = typeof params.session_id === 'string' ? params.session_id.trim() : ''

  const storedId = (paramId && (storedSessionIdForRuntimeId(paramId) ?? translatedRuntimeIds.get(paramId))) || paramId

  return { paramId, storedId }
}

/** The owner a connection-tagged row in the unified list names, when it is a
 *  registered connection other than `from`. */
function taggedForeignOwner(storedSessionId: string, from: string): SessionOwnerRoute | undefined {
  const registered = new Set(registryConnectionIds())

  const rows = [
    ...cachedSessionRowCandidates(storedSessionId),
    ...ownerLookupSessionRows().filter(row => sessionMatchesStoredId(row, storedSessionId))
  ]

  for (const row of rows) {
    const route = sessionOwnerRouteFromRow(row)

    if (route) {
      return route.connectionId !== from && registered.has(route.connectionId) ? route : undefined
    }
  }

  return undefined
}

function notFoundCode(error: unknown): number | undefined {
  const code = error && typeof error === 'object' ? (error as Partial<JsonRpcGatewayError>).code : undefined

  if (code === SESSION_NOT_FOUND_CODE || code === SESSION_NOT_IN_MEMORY_CODE) {
    return code
  }

  return undefined
}

export const sessionRpcOwnerFollower: SessionOwnerFollower = {
  redirect(owner, _method, params) {
    if (!multiGateway()) {
      return undefined
    }

    const { paramId, storedId } = rpcSessionIds(params)
    const from = routedConnectionId(owner)
    const followed = followedOwners.get(paramId) ?? followedOwners.get(storedId)

    if (followed && followed.from === from) {
      return followed.to
    }

    // The unified list's connection tag is the owning gateway's own claim; a
    // hint or tile route naming another gateway is a leftover (older builds
    // minted `local` for ids the selected gateway fabricated rows for).
    return storedId ? taggedForeignOwner(storedId, from) : undefined
  },

  async follow(owner, _method, params, error) {
    const code = notFoundCode(error)

    if (code === undefined || !multiGateway()) {
      return undefined
    }

    const { storedId } = rpcSessionIds(params)
    const from = routedConnectionId(owner)
    const key = `${storedId}|${from}`

    if (!storedId || (noForeignOwnerUntil.get(key) ?? 0) > now()) {
      return undefined
    }

    let lookup = ownerLookups.get(key)

    if (!lookup) {
      // 4007 (no stored session there) earns the by-id probe of the other
      // gateways; 4001 (runtime not in memory) is also what a reaped runtime on
      // its real owner answers, so only the cheap local records (unified list,
      // hints) may name another owner — never a probe per poll tick.
      lookup = findForeignSessionOwner(storedId, [from], { probe: code === SESSION_NOT_FOUND_CODE }).finally(() =>
        ownerLookups.delete(key)
      )
      ownerLookups.set(key, lookup)
    }

    const route = await lookup

    if (!route) {
      rememberBounded(noForeignOwnerUntil, key, now() + NO_FOREIGN_OWNER_TTL_MS)
    }

    return route
  },

  confirm(owner, route, _method, params) {
    const { paramId, storedId } = rpcSessionIds(params)
    const from = routedConnectionId(owner)

    for (const id of new Set([paramId, storedId])) {
      if (id) {
        rememberBounded(followedOwners, id, { from, to: route })
      }
    }

    if (storedId) {
      noForeignOwnerUntil.delete(`${storedId}|${from}`)
      pinSessionOwner(storedId, route)
      repointSessionTileOwner(storedId, route)
    }
  },

  reject(owner, _route, _method, params) {
    const { storedId } = rpcSessionIds(params)

    if (storedId) {
      rememberBounded(noForeignOwnerUntil, `${storedId}|${routedConnectionId(owner)}`, now() + NO_FOREIGN_OWNER_TTL_MS)
    }
  }
}

setSessionOwnerFollower(sessionRpcOwnerFollower)

/** Test seam: drop every follow memo. */
export function _resetSessionOwnerFollowForTests(): void {
  followedOwners.clear()
  noForeignOwnerUntil.clear()
  translatedRuntimeIds.clear()
  ownerLookups.clear()
}
