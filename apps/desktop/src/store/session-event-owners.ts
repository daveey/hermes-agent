import { type GatewayEvent, registryBackendScopeKey } from '@hermes/shared'
import { atom } from 'nanostores'

import { normalizeProfileKey } from './profile'
import { secondaryProfileOwnerForEvent } from './session-event-provenance'
import type { SessionOwnerScope } from './session-request-router'

const sessionScopeByRuntimeId = new Map<string, string>()

// Structured twin of the scope ledger: inbound events can carry either an
// exact (connectionId, profile) owner or a producer-proven profile-only pool
// owner. Consumed as the LAST rung of knownOwnerForSession so a runtime whose
// event source already proved its owner can still route session-scoped RPCs
// (approval.respond) when every durable binding (tile / hint / row) is absent
// — while durable stored identity keeps outranking it (#97511).
const sessionOwnerByRuntimeId = new Map<string, SessionOwnerScope>()
export const $sessionEventOwnersRevision = atom(0)

function setEventOwner(id: string, owner: SessionOwnerScope): void {
  if (JSON.stringify(sessionOwnerByRuntimeId.get(id)) === JSON.stringify(owner)) {
    return
  }

  sessionOwnerByRuntimeId.set(id, owner)
  $sessionEventOwnersRevision.set($sessionEventOwnersRevision.get() + 1)
}

export function recordSessionEventOwnerScope(event: {
  connectionId?: string
  profile?: string
  session_id?: string
}): void {
  if (!event.session_id) {
    return
  }

  if (event.connectionId) {
    sessionScopeByRuntimeId.set(event.session_id, registryBackendScopeKey(event.connectionId, event.profile))
    setEventOwner(event.session_id, {
      connectionId: event.connectionId,
      profile: String(event.profile ?? '').trim() || 'default'
    })

    // An owner resolved after the focus moved must still re-home the rail.

    return
  }

  // Only gateway.ts's secondary closure can add this non-serializable marker.
  // A profile field from a primary or arbitrary inbound event is descriptive,
  // not an owner route, and must keep failing closed in multi-profile installs.
  const profile = secondaryProfileOwnerForEvent(event as GatewayEvent)

  if (profile) {
    const profileKey = normalizeProfileKey(profile)
    setEventOwner(event.session_id, profileKey)
    sessionScopeByRuntimeId.set(event.session_id, profileKey)
  }
}

/** The owner an inbound runtime EVENT proved for `sessionId` (#97511): the
 *  exact (connectionId, profile) of the socket that delivered its events, or
 *  the bare profile of a legacy profile-only pool. Exported so the session-scoped
 *  RPC ladder can consult it WITHOUT the connection-blind profile rung
 *  preempting it (see knownOwnerForSession). */
export function runtimeSessionOwner(sessionId: null | string | undefined): SessionOwnerScope {
  const id = String(sessionId ?? '').trim()

  return id ? sessionOwnerByRuntimeId.get(id) : undefined
}

/** The composite source scope (connection + profile) a runtime's own events
 *  proved — `registryBackendScopeKey` of the socket that delivered them.
 *  Undefined for a runtime whose events arrived untagged (the local legacy
 *  primary), whose source is then whatever gateway is actively serving this
 *  window. Reaction-overlay reads key the displayed session's scope with
 *  this, so an overlay recorded on one source never paints another
 *  source's same-numbered row. */
export function sessionEventScopeFor(runtimeId: null | string | undefined): string | undefined {
  const id = String(runtimeId ?? '').trim()

  return id ? sessionScopeByRuntimeId.get(id) : undefined
}

/** Forget only profile-pool runtime owners during permanent LOCAL profile
 * teardown. These string routes came exclusively from the legacy secondary
 * producer; exact connection descriptors must survive a same-named remote
 * profile's local delete/rename. */
export function forgetProfileOnlyRuntimeOwners(profile: string): void {
  const retired = normalizeProfileKey(profile)

  for (const [runtimeId, owner] of sessionOwnerByRuntimeId) {
    if (typeof owner === 'string' && normalizeProfileKey(owner) === retired) {
      sessionOwnerByRuntimeId.delete(runtimeId)
      $sessionEventOwnersRevision.set($sessionEventOwnersRevision.get() + 1)
      sessionScopeByRuntimeId.delete(runtimeId)
    }
  }
}

export function forgetRuntimeEventOwner(runtimeId: string): void {
  sessionScopeByRuntimeId.delete(runtimeId)
  sessionOwnerByRuntimeId.delete(runtimeId)
  $sessionEventOwnersRevision.set($sessionEventOwnersRevision.get() + 1)
}

export function clearRuntimeEventOwners(): void {
  sessionScopeByRuntimeId.clear()
  sessionOwnerByRuntimeId.clear()
  $sessionEventOwnersRevision.set($sessionEventOwnersRevision.get() + 1)
}
