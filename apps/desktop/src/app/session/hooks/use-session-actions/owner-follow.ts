import type { JsonRpcGatewayError } from '@hermes/shared'

import { getSession } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
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
  type SessionOwnerRoute,
  sessionOwnerRouteFromRow,
  type SessionOwnerScope
} from '@/store/session-request-router'

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
  return ($connectionsRegistry.get()?.connections ?? [])
    .map(connection => connection.id?.trim() ?? '')
    .filter(Boolean)
}

/**
 * The owner of `storedSessionId` on a connection OTHER than `excluded`, from
 * the unified session list (every connection's connection-tagged rows), then
 * the persisted owner hints, then a read-only by-id probe of each remaining
 * registry connection. Undefined when no other connection has it — or when
 * there is no other connection at all (single-gateway installs never probe).
 */
export async function findForeignSessionOwner(
  storedSessionId: string,
  excluded: readonly string[]
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

  const candidates = registryConnectionIds().filter(connectionId => !skip.has(connectionId))

  if (candidates.length === 0 || registryConnectionIds().length < 2) {
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
