import type { ProfileScope } from '@/hermes'
import { requestGatewayForAgent, requestGatewayForProfile, retainGatewayForSessionTurn } from '@/store/gateway'

import { resetBackgroundPollingGuardAfterRebind } from './session-gone-latch'

/**
 * The ONE authoritative exact owner of a session: the registry connection whose
 * socket minted (or resumed) the runtime, plus the Desktop profile that selects
 * that route. `targetProfile` is the backend profile the route serves when it
 * differs from the Desktop-side name (remote overrides); `mode` is informative.
 *
 * Captured ONCE at the new-chat intent / send linearization point
 * (store/profile resolveNewChatOwnerRoute) and carried through session.create,
 * the owner hint, the optimistic row, the runtime binding, the foreground hold
 * and every later session-scoped RPC. Never re-derived from ambient state after
 * an asynchronous activation: connection/profile EQUALITY is not enough — the
 * runtime lives on one concrete WebSocket, and only this route names the
 * registry entry that holds it.
 */
export interface SessionOwnerRoute {
  connectionId: string
  mode?: 'local' | 'remote'
  profile: string
  targetProfile?: string
}

/** @deprecated Alias kept for existing imports; new code names SessionOwnerRoute. */
export type SessionProfileRoute = SessionOwnerRoute

export type SessionOwnerScope = undefined | null | string | SessionOwnerRoute

/** REST scope for a session owner. Undefined when the owner is unknown. */
export function profileScopeForSessionOwner(owner: SessionOwnerScope): ProfileScope {
  if (!owner) {
    return undefined
  }

  if (typeof owner === 'string') {
    return owner
  }

  return {
    connectionId: owner.connectionId,
    profile: owner.targetProfile ?? owner.profile
  }
}

/** Exact owner reconstructed from a CONNECTION-TAGGED session row (the
 *  Electron unified-list splice tags foreign registry rows; an optimistic row
 *  carries the create route's connection; mergeSessionPage carries the tag
 *  across refreshes). A row without a connection tag yields undefined — a bare
 *  profile is not an exact owner. */
export function sessionOwnerRouteFromRow(
  row: { connection_id?: null | string; profile?: null | string } | null | undefined
): SessionOwnerRoute | undefined {
  const connectionId = String(row?.connection_id ?? '').trim()

  if (!connectionId) {
    return undefined
  }

  return { connectionId, profile: String(row?.profile ?? '').trim() || 'default' }
}

// ── Session-scoped RPC routing (the #89206 class) ───────────────────────────
// A session-scoped RPC (session.resume / session.activate / session.usage /
// prompt.submit) only means anything on the backend that OWNS the session's
// profile. A session's profile is a PROPERTY OF THE SESSION, not of whatever
// the window is currently showing. The "active gateway" is a moving target
// (a concurrent switch, an idle-reap eviction, a failed dial, or a connection
// edit re-points it) AND, for a hidden/unlisted session, it is simply the
// WRONG backend — one that never owned the session. Dispatching there 404s or
// times out while the session's own backend is healthy (blank Bot Chats, dead
// wake-ups; local pool and SSH alike).
//
// So: a KNOWN owner is always routed to its own profile's socket — there is no
// "same as active, so use ambient" shortcut, because "active" carries no
// routing authority. Only a genuinely UNKNOWN owner (a fresh draft with no
// session yet, or truly global chrome) falls to the ambient dispatcher, and
// callers are expected to resolve the owner (cross-profile probe) before they
// reach that case for a real session.

const normKey = (profile: null | string | undefined): string => (profile ?? '').trim() || 'default'

export const isSessionOwnerRoute = (owner: SessionOwnerScope): owner is SessionOwnerRoute =>
  Boolean(owner && typeof owner === 'object' && 'connectionId' in owner)

const isRoute = isSessionOwnerRoute

function routeParams(route: SessionProfileRoute, params: Record<string, unknown>): Record<string, unknown> {
  if (!route.targetProfile || !Object.prototype.hasOwnProperty.call(params, 'profile')) {
    return params
  }

  return { ...params, profile: route.targetProfile }
}

function promptSessionId(method: string, params: Record<string, unknown>): string {
  return method === 'prompt.submit' && typeof params.session_id === 'string' ? params.session_id.trim() : ''
}

const TERMINAL_TURN_ACK_STATUSES = new Set(['complete', 'completed', 'error'])

function turnKeepsRunning(result: unknown): boolean {
  if (!result || typeof result !== 'object' || !('status' in result)) {
    // Older gateways may ACK without the newer structured status. Retaining
    // until the terminal event is safer than recreating the client-gone cut.
    return true
  }

  const status = (result as { status?: unknown }).status

  // Queued, redirected and future status values are non-terminal by default.
  // Releasing only an explicit terminal ACK avoids recreating client_gone
  // when a gateway accepts a turn without calling it "streaming".
  return typeof status !== 'string' || !TERMINAL_TURN_ACK_STATUSES.has(status)
}

async function withRoutedTurnLease<T>(
  connectionId: null | string,
  profile: string,
  method: string,
  params: Record<string, unknown>,
  request: () => Promise<T>
): Promise<T> {
  const sessionId = promptSessionId(method, params)

  if (!sessionId) {
    return requestWithRebindGuard(method, params, request)
  }

  const release = await retainGatewayForSessionTurn(connectionId, profile, sessionId)

  try {
    const result = await request()
    resetBackgroundPollingGuardAfterRebind(method, params, result)

    if (!turnKeepsRunning(result)) {
      release()
    }

    return result
  } catch (error) {
    release()
    throw error
  }
}

async function requestWithRebindGuard<T>(
  method: string,
  params: Record<string, unknown>,
  request: () => Promise<T>
): Promise<T> {
  const result = await request()
  resetBackgroundPollingGuardAfterRebind(method, params, result)

  return result
}

/**
 * True when a session-scoped RPC must be pinned to `ownerProfile`'s own socket.
 *
 * A KNOWN owner (route or profile name) always needs its own socket: the
 * session belongs to that profile regardless of what the window is showing.
 * A bare profile names the legacy profile door's pool socket in every
 * topology (a pick on the primary / explicit `local` source dials it).
 * There is deliberately NO comparison against the active profile — "active" is
 * presentation state, never a routing authority. Only a null/empty owner (a
 * fresh draft with no session, or global chrome) routes ambient.
 */
export function sessionRpcNeedsProfileRoute(ownerProfile: SessionOwnerScope | undefined): boolean {
  if (isRoute(ownerProfile)) {
    // A descriptor is an immutable ownership claim. Even an explicitly local
    // route must not collapse to the ambient request: another connection can
    // expose the same profile name, and activation is UI state only.
    return Boolean(ownerProfile.connectionId.trim())
  }

  return ownerProfile != null && Boolean(String(ownerProfile).trim())
}

// ── Following a session to its owning gateway [daveey] ──────────────────────
// With several registry gateways in one window, an owner record can name the
// wrong gateway (a stale hint or tile route from before the session's owner was
// known). That gateway answers the RPC "session not found" — 4007 for a stored
// id, 4001 ("not in memory") for a runtime id — and background pollers then hit
// it every few seconds. The follower (registered by the session layer, which
// owns the unified list, the hints and the registry probe) corrects the route
// before dispatch when the unified list proves another owner, and after such a
// not-found answer resolves the owner and retries ONCE there. session.resume is
// never followed here: its callers rebind the runtime to the owner themselves.

type SessionRpcParams = Record<string, unknown>

export interface SessionOwnerFollower {
  /** Sync, pre-dispatch: a proven better owner than `owner`, else undefined. */
  redirect: (owner: SessionOwnerScope, method: string, params: SessionRpcParams) => SessionOwnerRoute | undefined
  /** After `owner` failed with `error`: the one owner to retry on, else undefined. */
  follow: (
    owner: SessionOwnerScope,
    method: string,
    params: SessionRpcParams,
    error: unknown
  ) => Promise<SessionOwnerRoute | undefined>
  /** The retry on `route` answered: record it so the wrong owner is not tried again. */
  confirm: (owner: SessionOwnerScope, route: SessionOwnerRoute, method: string, params: SessionRpcParams) => void
  /** The retry on `route` failed too: stop following for this session/owner pair. */
  reject: (owner: SessionOwnerScope, route: SessionOwnerRoute, method: string, params: SessionRpcParams) => void
}

let sessionOwnerFollower: SessionOwnerFollower | null = null

export function setSessionOwnerFollower(next: SessionOwnerFollower | null): void {
  sessionOwnerFollower = next
}

function followableSessionRpc(method: string, params: SessionRpcParams): boolean {
  return method !== 'session.resume' && typeof params.session_id === 'string' && Boolean(params.session_id.trim())
}

/**
 * Dispatch a session-scoped RPC on the socket that owns `ownerProfile`,
 * falling back to the ambient dispatcher when the active gateway already
 * serves that profile (keeps the primary's reauth-aware reconnect path).
 * The route is decided at CALL time, not at swap time. With a follower
 * registered (multi-gateway), the route is corrected / followed once — see
 * SessionOwnerFollower.
 */
export function requestForSessionProfile<T>(
  ownerProfile: SessionOwnerScope | undefined,
  ambientRequest: <R>(
    method: string,
    params?: Record<string, unknown>,
    timeoutMs?: number,
    signal?: AbortSignal
  ) => Promise<R>,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<T> {
  const follower = sessionOwnerFollower

  if (!follower || !followableSessionRpc(method, params)) {
    return dispatchForSessionOwner<T>(ownerProfile, ambientRequest, method, params, timeoutMs, signal)
  }

  const owner: SessionOwnerScope = follower.redirect(ownerProfile, method, params) ?? ownerProfile

  return dispatchForSessionOwner<T>(owner, ambientRequest, method, params, timeoutMs, signal).catch(
    async (error: unknown) => {
      const route = signal?.aborted ? undefined : await follower.follow(owner, method, params, error)

      if (!route) {
        throw error
      }

      try {
        const result = await dispatchForSessionOwner<T>(route, ambientRequest, method, params, timeoutMs, signal)
        follower.confirm(owner, route, method, params)

        return result
      } catch {
        follower.reject(owner, route, method, params)

        // The caller's error handling (gone-latch, not-found recovery) keys on
        // the answer from the route it chose.
        throw error
      }
    }
  )
}

function dispatchForSessionOwner<T>(
  ownerProfile: SessionOwnerScope | undefined,
  ambientRequest: <R>(
    method: string,
    params?: Record<string, unknown>,
    timeoutMs?: number,
    signal?: AbortSignal
  ) => Promise<R>,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<T> {
  if (isRoute(ownerProfile)) {
    const connectionId = ownerProfile.connectionId.trim()

    if (!connectionId) {
      return Promise.reject(new Error('Session owner route is missing connectionId'))
    }

    const routedParams = routeParams(ownerProfile, params)

    const profile = normKey(ownerProfile.profile)

    return withRoutedTurnLease(connectionId, profile, method, routedParams, () =>
      timeoutMs === undefined && signal === undefined
        ? requestGatewayForAgent<T>(connectionId, profile, method, routedParams)
        : requestGatewayForAgent<T>(connectionId, profile, method, routedParams, timeoutMs, signal)
    )
  }

  if (!sessionRpcNeedsProfileRoute(ownerProfile)) {
    // Forward the extra args only when the caller actually supplied them. The
    // ambient dispatcher is a plain gateway request whose arity callers assert
    // on; handing it a trailing `undefined, undefined` on every session RPC
    // changes the observed call shape for the many callers that never asked
    // for a deadline (the plugin host bridge in contrib/wiring is the only one
    // that does).
    if (signal !== undefined) {
      return requestWithRebindGuard(method, params, () => ambientRequest<T>(method, params, timeoutMs, signal))
    }

    if (timeoutMs !== undefined) {
      return requestWithRebindGuard(method, params, () => ambientRequest<T>(method, params, timeoutMs))
    }

    return requestWithRebindGuard(method, params, () => ambientRequest<T>(method, params))
  }

  const profile = normKey(ownerProfile)

  return withRoutedTurnLease(null, profile, method, params, () =>
    requestGatewayForProfile<T>(profile, method, params, timeoutMs, signal)
  )
}
