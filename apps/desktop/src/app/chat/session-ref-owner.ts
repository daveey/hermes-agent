/**
 * Which backend owns the session a `@session:` reference points at.
 *
 * A ref click used to open the bare id against whichever gateway was active.
 * On a multi-gateway Desktop that is often the wrong one: with a remote
 * gateway (e.g. Nous) selected, clicking `@session:default/<local id>` opened a
 * tile that probed only the remote, found nothing, and stayed blank. The ref
 * carries enough to route it, so resolve the owner before opening:
 *
 *   1. `<profile>@<gateway>/<id>` — the gateway qualifier names the owner
 *      (registry id or slugged label, the form session-mentions inserts).
 *   2. A unified-list row for the id: its `connection_id` tag, or the registry
 *      primary when untagged (only the primary's own rows are left bare).
 *   3. The remembered owner hint for the id.
 *   4. An unqualified ref is relative to the chat it was written in, so the
 *      owner of the session that shows the link (the origin).
 *
 * Nothing known → undefined, and the caller keeps the ambient behaviour.
 */
import { tileOwnerRoute } from '@/app/chat/session-tile-owner'
import { parseSessionRefValue } from '@/lib/session-refs'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { normalizeProfileKey } from '@/store/profile'
import { $sessions, getSessionOwnerHint, sessionMatchesStoredId } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'
import { $sessionTiles } from '@/store/session-states'

const slug = (text: string): string =>
  text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')

/** Registry connection id for a ref's gateway qualifier (id or slugged label). */
export function registryConnectionIdForGateway(gateway: string): string | undefined {
  const wanted = gateway.trim().toLowerCase()

  if (!wanted) {
    return undefined
  }

  const connections = $connectionsRegistry.get()?.connections ?? []

  return (
    connections.find(entry => entry.id.toLowerCase() === wanted) ??
    connections.find(entry => slug(entry.label || '') === slug(wanted))
  )?.id
}

export function resolveSessionRefOwner(value: string, originStoredId?: null | string): SessionOwnerRoute | undefined {
  const { gateway, profile, sessionId } = parseSessionRefValue(value)

  if (!sessionId) {
    return undefined
  }

  const refProfile = profile?.trim() ? normalizeProfileKey(profile) : undefined
  const profileMatches = (candidate?: null | string) => !refProfile || normalizeProfileKey(candidate) === refProfile

  const route = (connectionId: string, ownerProfile?: null | string): SessionOwnerRoute => {
    const key = ownerProfile?.trim() ? normalizeProfileKey(ownerProfile) : (refProfile ?? 'default')

    return { connectionId, profile: key, targetProfile: key }
  }

  if (gateway) {
    const connectionId = registryConnectionIdForGateway(gateway)

    return connectionId ? route(connectionId) : undefined
  }

  const listed = $sessions.get().filter(row => sessionMatchesStoredId(row, sessionId) && profileMatches(row.profile))
  const tagged = listed.find(row => row.connection_id?.trim())

  if (tagged?.connection_id) {
    return route(tagged.connection_id.trim(), tagged.profile)
  }

  // An UNTAGGED unified-list row is the primary's own (rows served by any other
  // source are stamped with their connection — `stampRowsWithOwningConnection`
  // and the registry splice). Name the primary explicitly: left bare, the tile
  // probes whichever gateway is active, which is how a local session opened
  // blank while a cloud gateway was selected.
  const registry = $connectionsRegistry.get()
  const untagged = listed.find(row => !row.connection_id?.trim())

  if (untagged && registry?.primary) {
    return route(registry.primary, untagged.profile)
  }

  const hint = getSessionOwnerHint(sessionId)

  if (hint && profileMatches(hint.targetProfile || hint.profile)) {
    return hint
  }

  if (originStoredId && originStoredId !== sessionId) {
    const origin = tileOwnerRoute($sessionTiles.get(), $sessions.get(), originStoredId)

    if (origin?.connectionId) {
      return route(origin.connectionId)
    }
  }

  return undefined
}
