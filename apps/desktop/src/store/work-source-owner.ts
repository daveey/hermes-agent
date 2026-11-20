/** Captured work authority. Unknown owners and unavailable transports never certify reads. */
import { atom } from 'nanostores'

import { $activeGatewayRoute, gatewayRouteState, pinnedPrimaryGatewayConnectionId } from '@/store/gateway'
import { $gatewayActivationEpoch } from '@/store/gateway-activation-epoch'
import { $primaryGatewayRoute } from '@/store/gateway-primary-route'
import { $projectTree } from '@/store/project-tree'
import {
  $activeSessionId,
  $cronSessions,
  $messagingSessions,
  $selectedStoredSessionId,
  $sessionOwnerHintsRevision,
  $sessions,
  $unlistedSessionOwnerRows,
  ownerLookupSessionRows
} from '@/store/session'
import { isSessionOwnerRoute, type SessionOwnerScope } from '@/store/session-request-router'
import {
  $sessionEventOwnersRevision,
  $sessionStates,
  $sessionTiles,
  knownOwnerForSession,
  storedSessionIdForRuntimeId
} from '@/store/session-states'
import type { WorkSourceAuthority } from '@/store/session-work-source'
import type { SessionInfo } from '@/types/hermes'

export interface QualifiedWorkSourceOwner {
  connectionId: string
  profile: string
  routeKey: string
}

type Socket = {
  connectionState?: string
  onState?: (handler: (state: string) => void) => () => void
}

interface Tracked {
  owner: QualifiedWorkSourceOwner
  storedSessionId: string
  semantic: string
  runtimes: string
  durable: number
  runtime: number
  socket: Socket | null
  socketState: string | null
  offState?: () => void
}

interface Candidate {
  exact: boolean
  row: SessionInfo
}

const tracked = new Map<string, Tracked>()
const unsubscribers: Array<() => void> = []
const TRACKED_LIMIT = 4096
let epoch = 0
let runtimeIndex = new Map<string, string>()
let ownerRows: readonly SessionInfo[] | undefined
let ownerRowIndex = new Map<string, Candidate[]>()
let ownerIndexBuilds = 0
let mirrorBindings = ''
export const $workSourceAuthorityRevision = atom(1)

function nextEpoch(): number {
  if (epoch === Number.MAX_SAFE_INTEGER) {
    throw new Error('Work authority epoch exhausted')
  }

  return ++epoch
}

function bumpRevision(): void {
  $workSourceAuthorityRevision.set($workSourceAuthorityRevision.get() + 1)
}

function scopeKey(owner: QualifiedWorkSourceOwner, stored: string): string {
  return JSON.stringify([owner.connectionId, owner.profile, stored])
}

function semanticOf(owner: QualifiedWorkSourceOwner | undefined): string {
  return owner ? JSON.stringify([owner.routeKey, owner.connectionId, owner.profile]) : ''
}

/** A bare profile pins only the published real primary; never the ambient gateway. */
export function qualifiedWorkSourceOwner(owner: SessionOwnerScope): QualifiedWorkSourceOwner | undefined {
  if (isSessionOwnerRoute(owner)) {
    const connectionId = owner.connectionId.trim()
    const profile = owner.profile.trim() || 'default'

    if (!connectionId) {
      return undefined
    }

    return { connectionId, profile, routeKey: gatewayRouteState(connectionId, profile).key }
  }

  if (typeof owner !== 'string' || !owner.trim()) {
    return undefined
  }

  const profile = owner.trim()
  const { key } = gatewayRouteState(null, profile)
  const connectionId = pinnedPrimaryGatewayConnectionId()

  if (key !== `primary:${profile}` || !connectionId) {
    return undefined
  }

  return { connectionId, profile, routeKey: key }
}

function invalidate(entry: Tracked, durable: boolean): void {
  if (durable) {
    entry.durable = nextEpoch()
  }

  entry.runtime = nextEpoch()
  bumpRevision()
}

function bindSocket(entry: Tracked, socket: Socket | null): void {
  if (entry.socket === socket) {
    return
  }

  entry.offState?.()
  entry.offState = undefined
  entry.socket = socket
  entry.socketState = socket?.connectionState ?? null
  invalidate(entry, true)
  entry.offState = socket?.onState?.(state => {
    if (entry.socketState === state) {
      return
    }

    entry.socketState = state
    invalidate(entry, true)
  })
}

/** Every row naming an id (own id or lineage alias), in resolver list order. Built once per row-list identity. */
function candidateIndex(): Map<string, Candidate[]> {
  const rows = ownerLookupSessionRows()

  if (rows !== ownerRows) {
    ownerRows = rows
    ownerRowIndex = new Map()
    ownerIndexBuilds++

    for (const row of rows) {
      const seen = new Set<string>()

      for (const alias of [row.id, row._lineage_root_id, ...(row._lineage_ids ?? [])]) {
        if (!alias || seen.has(alias)) {
          continue
        }

        seen.add(alias)
        const list = ownerRowIndex.get(alias)
        const candidate = { exact: alias === row.id, row }

        if (list) {
          list.push(candidate)
        } else {
          ownerRowIndex.set(alias, [candidate])
        }
      }
    }
  }

  return ownerRowIndex
}

/** Rows competing for an id: those of the first match's kind (own id vs lineage alias), preserving list precedence. */
function competingRows(stored: string): SessionInfo[] {
  const list = candidateIndex().get(stored)

  if (!list?.length) {
    return []
  }

  const exact = list[0].exact

  return list.filter(candidate => candidate.exact === exact).map(candidate => candidate.row)
}

/**
 * Owners the resolver can name for `id`. One row (or none) keeps the resolver's
 * first-match behavior; several same-ID rows on different routes each resolve on
 * their own, so a foreign route's row never shadows another healthy route.
 */
function candidateOwners(id: string): Array<QualifiedWorkSourceOwner | undefined> {
  const stored = storedSessionIdForRuntimeId(id) ?? id
  const rows = competingRows(stored)

  if (rows.length <= 1) {
    return [qualifiedWorkSourceOwner(knownOwnerForSession(id, rows))]
  }

  return rows.map(row => qualifiedWorkSourceOwner(knownOwnerForSession(id, [row])))
}

/** The candidate for exactly this entry's scope, else the first candidate (which then fails the scope match). */
function ownerForEntry(entry: Tracked): QualifiedWorkSourceOwner | undefined {
  const owners = candidateOwners(entry.storedSessionId)

  return (
    owners.find(
      owner => owner?.connectionId === entry.owner.connectionId && owner?.profile === entry.owner.profile
    ) ?? owners[0]
  )
}

function syncEntry(entry: Tracked): void {
  const resolved = ownerForEntry(entry)
  const semantic = semanticOf(resolved)
  const runtimes = runtimeIndex.get(scopeKey(entry.owner, entry.storedSessionId)) ?? ''

  if (semantic !== entry.semantic) {
    entry.semantic = semantic
    entry.runtimes = runtimes
    invalidate(entry, true)
  } else if (runtimes !== entry.runtimes) {
    entry.runtimes = runtimes
    invalidate(entry, false)
  }

  const matches = resolved?.connectionId === entry.owner.connectionId && resolved?.profile === entry.owner.profile
  bindSocket(entry, matches ? gatewayRouteState(resolved.connectionId, resolved.profile).gateway : null)
}

/**
 * Resolve each runtime once per binding update, not once per observed scope.
 * The index value is the runtime's effective ROLES (mirror / tile / main), not
 * mere membership: rebinding a tile or the main pane between runtimes that
 * already have mirrors must still change what a cached proof was read against.
 */
function rebuildRuntimeIndex(): void {
  const roles = new Map<string, string>()
  const addRole = (id: string, role: string) => roles.set(id, (roles.get(id) ?? '') + role)

  for (const id of Object.keys($sessionStates.get())) {
    addRole(id, 'm')
  }

  for (const tile of $sessionTiles.get()) {
    if (tile.runtimeId) {
      addRole(tile.runtimeId, 't')
    }
  }

  const active = $activeSessionId.get()

  if (active) {
    addRole(active, 'a')
  }

  const index = new Map<string, Array<[string, string]>>()

  for (const [id, role] of roles) {
    const stored = storedSessionIdForRuntimeId(id)

    if (!stored) {
      continue
    }

    // A runtime of an ambiguous same-ID row set may serve any of those routes:
    // attribute it to each so a rebinding can only over-revoke, never miss.
    for (const owner of candidateOwners(id)) {
      if (!owner) {
        continue
      }

      const key = scopeKey(owner, stored)
      const values = index.get(key) ?? []

      if (!values.some(([existing]) => existing === id)) {
        values.push([id, role])
      }

      index.set(key, values)
    }
  }

  runtimeIndex = new Map([...index].map(([key, values]) => [key, JSON.stringify(values.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))]))
}

function syncTracked(): void {
  rebuildRuntimeIndex()

  for (const entry of tracked.values()) {
    syncEntry(entry)
  }
}

function syncMirrors(): void {
  // Token streaming changes the state objects, but not these bindings. Avoid
  // resolver scans and repaint publication for that hot path.
  const bindings = JSON.stringify(
    Object.entries($sessionStates.get()).map(([id, state]) => [id, state.storedSessionId])
  )

  if (bindings === mirrorBindings) {
    return
  }

  mirrorBindings = bindings
  syncTracked()
}

function ensureObservers(): void {
  if (unsubscribers.length) {
    return
  }

  unsubscribers.push(
    $sessionOwnerHintsRevision.listen(syncTracked),
    $sessionEventOwnersRevision.listen(syncTracked),
    $sessionStates.listen(syncMirrors),
    $sessionTiles.listen(syncTracked),
    $sessions.listen(syncTracked),
    $cronSessions.listen(syncTracked),
    $messagingSessions.listen(syncTracked),
    $unlistedSessionOwnerRows.listen(syncTracked),
    $projectTree.listen(syncTracked),
    $primaryGatewayRoute.listen(syncTracked),
    $gatewayActivationEpoch.listen(syncTracked),
    $activeGatewayRoute.listen(syncTracked),
    $activeSessionId.listen(syncTracked),
    $selectedStoredSessionId.listen(syncTracked)
  )
  mirrorBindings = JSON.stringify(
    Object.entries($sessionStates.get()).map(([id, state]) => [id, state.storedSessionId])
  )
  rebuildRuntimeIndex()
}

export function workSourceAuthorityForSession(
  connectionId: string | undefined,
  profile: string | undefined,
  storedSessionId: string | undefined
): WorkSourceAuthority {
  const none: WorkSourceAuthority = { durable: null, runtime: null }

  if (!storedSessionId?.trim() || !profile?.trim()) {
    return none
  }

  const requested = qualifiedWorkSourceOwner(connectionId ? { connectionId, profile } : profile)

  if (!requested) {
    return none
  }

  ensureObservers()
  const key = scopeKey(requested, storedSessionId)
  let entry = tracked.get(key)

  if (!entry) {
    entry = {
      owner: requested,
      storedSessionId,
      semantic: '',
      runtimes: '',
      durable: nextEpoch(),
      runtime: nextEpoch(),
      socket: null,
      socketState: null
    }
  }

  // LRU eviction discards the socket subscription as well as the cached token.
  // Epoch allocation never resets, so an evicted authority cannot revive.
  tracked.delete(key)
  tracked.set(key, entry)

  if (tracked.size > TRACKED_LIMIT) {
    const oldest = tracked.keys().next().value!
    tracked.get(oldest)?.offState?.()
    tracked.delete(oldest)
  }

  syncEntry(entry)

  if (!entry.semantic || !entry.socket || entry.socketState !== 'open') {
    return none
  }

  return { durable: entry.durable, runtime: entry.runtime }
}

/** Read-only render snapshot. Observation is installed by SDK reads or a mount effect. */
export function peekWorkSourceAuthorityForSession(
  connectionId: string,
  profile: string,
  storedSessionId: string
): WorkSourceAuthority {
  const entry = tracked.get(scopeKey({ connectionId, profile, routeKey: '' }, storedSessionId))

  if (!entry?.semantic || !entry.socket || entry.socketState !== 'open') {
    return { durable: null, runtime: null }
  }

  return { durable: entry.durable, runtime: entry.runtime }
}

/** Test-only: how many times the owner-row candidate index was rebuilt. */
export function _ownerIndexBuildsForTests(): number {
  return ownerIndexBuilds
}

/** Test-only: drop listeners but never recycle request authority. */
export function _resetWorkSourceOwnerForTests(): void {
  ownerIndexBuilds = 0

  for (const entry of tracked.values()) {
    entry.offState?.()
  }

  tracked.clear()
  runtimeIndex.clear()
  ownerRows = undefined
  ownerRowIndex.clear()
  mirrorBindings = ''

  while (unsubscribers.length) {
    unsubscribers.pop()?.()
  }

  $workSourceAuthorityRevision.set(1)
}
