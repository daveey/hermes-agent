/**
 * hermes-hub replica access for the Desktop [daveey].
 *
 * Every gateway that runs hermes-hub keeps a local replica (hub.db) of every
 * gateway's sessions, Inbox cards and Kanban cards, served read-only by the
 * `hub-sync` backend plugin at `/api/plugins/hub-sync/*` (hermes-mods). This
 * module is the Desktop's door to it:
 *
 *  - reads go to the window's PRIMARY connection (its own gateway, which keeps
 *    answering while the hub or the other gateways are down);
 *  - hub gateway names ("mini", "macbook") are mapped to registry connection
 *    ids by asking each reachable connection's own `/status` (persisted, so a
 *    gateway that is offline now still maps), with an id/label/host match as
 *    the fallback;
 *  - every call is best-effort: a gateway without the plugin, without
 *    hermes-hub, or unreachable answers `null`, and callers keep their live
 *    paths. Nothing here ever throws into a caller.
 */
import { pluginRest } from '@/api/plugins'
import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import type { SessionOwnerRoute } from '@/store/session-request-router'

export const HUB_SYNC_PLUGIN = 'hub-sync'
export const HUB_GATEWAY_NAMES_KEY = 'hermes.desktop.hubGatewayNames.v1'
const READ_TIMEOUT_MS = 4_000
const STATUS_TTL_MS = 5 * 60_000
const WHERE_TTL_MS = 30_000

export interface HubPending {
  id: string
  action: string
  state: string
  created_at?: number
}

/** One replica record as hub-sync serves it: the owner's data plus where it
 *  lives and how fresh it is. `local` = this (primary) gateway's own record. */
export interface HubRecord {
  gateway: string
  key: string
  version: null | number
  local: boolean
  stale: boolean
  /** Epoch seconds: the newest moment the owner's state is known for. */
  synced_at: null | number
  pending?: HubPending[]
  [field: string]: unknown
}

export interface HubTask extends HubRecord {
  id: string
  board?: string
  title?: string
  body?: null | string
  status?: string
  priority?: number
  assignee?: null | string
  created_at?: number
  started_at?: null | number
  completed_at?: null | number
  comments?: number
  run?: null | { status?: string; started_at?: number; summary?: string }
}

export interface HubWhereHit {
  gateway: string
  profile?: string
  session?: string
  local?: boolean
  stale?: boolean
  synced_at?: null | number
  archived?: boolean
}

export interface HubRequestBody {
  gateway: string
  coll: 'inbox' | 'projects' | 'sessions' | 'tasks'
  key: string
  action: string
  args?: Record<string, unknown>
  base_version?: null | number
}

export interface HubRequestResult {
  id: string
  queued_locally: boolean
  state: string
}

/** The connection whose replica the Desktop reads: the window's own gateway. */
export function hubReplicaConnectionId(): string {
  return $connectionsRegistry.get()?.primary?.trim() || 'local'
}

function registryConnections() {
  return $connectionsRegistry.get()?.connections ?? []
}

// ── gateway name ↔ connection id ────────────────────────────────────────────

// In-memory copy for when localStorage is unavailable; storage is re-read on
// every lookup because the hub-sync desktop plugin learns names too.
let memoryNames: Record<string, string> = {}

function loadNames(): Record<string, string> {
  const out: Record<string, string> = { ...memoryNames }

  try {
    const raw = globalThis.localStorage?.getItem(HUB_GATEWAY_NAMES_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : null

    if (parsed && typeof parsed === 'object') {
      for (const [connectionId, gateway] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof gateway === 'string' && gateway.trim()) {
          out[connectionId] = gateway.trim()
        }
      }
    }
  } catch {
    // Corrupt or unavailable storage: the in-memory copy serves.
  }

  return out
}

/** Remember that `connectionId` is hub gateway `gateway` (its own /status said so). */
export function rememberHubGatewayName(connectionId: string, gateway: string): void {
  const map = loadNames()
  const id = connectionId.trim()
  const name = gateway.trim()

  if (!id || !name || map[id] === name) {
    return
  }

  // One connection per hub name: a re-added connection takes the name over.
  for (const [other, value] of Object.entries(map)) {
    if (value === name) {
      delete map[other]
    }
  }

  map[id] = name
  memoryNames = map

  try {
    globalThis.localStorage?.setItem(HUB_GATEWAY_NAMES_KEY, JSON.stringify(map))
  } catch {
    // Quota/private mode: the in-memory map still serves this window.
  }
}

const norm = (value: unknown): string =>
  String(value ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '')

function guessConnection(gateway: string): string | undefined {
  const want = norm(gateway)

  if (!want) {
    return undefined
  }

  const taken = new Set(Object.keys(loadNames()))

  const hit = registryConnections().find(connection => {
    if (taken.has(connection.id)) {
      return false // this connection already proved another hub name
    }

    const host = String((connection as { host?: string }).host ?? '').split('.')[0]

    return [connection.id, connection.label, host].some(candidate => norm(candidate) === want)
  })

  return hit?.id
}

/** Registry connection id of hub gateway `gateway`, or undefined when this
 *  Desktop has no connection for it (an unregistered gateway). */
export function connectionForHubGateway(gateway: string): string | undefined {
  const name = gateway.trim()
  const registered = new Set(registryConnections().map(connection => connection.id))
  const known = Object.entries(loadNames()).find(([id, value]) => value === name && registered.has(id))

  return known?.[0] ?? guessConnection(name)
}

/** Hub gateway name of registry connection `connectionId`, when known. */
export function hubGatewayForConnection(connectionId: string): string | undefined {
  return loadNames()[connectionId.trim()]
}

// ── REST ────────────────────────────────────────────────────────────────────

/** GET a hub-sync path; null when unavailable (no plugin, no hermes-hub, error). */
export async function hubGet<T extends object>(
  path: string,
  connectionId: string = hubReplicaConnectionId()
): Promise<null | T> {
  try {
    const result = await pluginRest<T & { available?: boolean }>(HUB_SYNC_PLUGIN, path, {
      connectionId,
      passive: true,
      timeoutMs: READ_TIMEOUT_MS
    })

    if (!result || typeof result !== 'object' || result.available === false) {
      return null
    }

    // Every hub-sync answer names the gateway that served it.
    const served = (result as { gateway?: unknown }).gateway

    if (typeof served === 'string') {
      rememberHubGatewayName(connectionId, served)
    }

    return result
  } catch {
    return null
  }
}

const statusChecked = new Map<string, number>()
/** [daveey] A /status read in flight per connection: concurrent askers (every board poll's answering gateways,
 *  project routing) share it instead of each opening one. */
const statusInFlight = new Map<string, Promise<boolean>>()
/** [daveey] When a connection's /status last failed or said "no hub-sync": not asked again for STATUS_RETRY_MS.
 *  Board polls ask every answering gateway each 15 s, so a gateway without hub-sync (or a slow one) was asked on
 *  every poll: 501 hub-sync/status timeouts in 30 min on 2026-11-13. */
const statusFailedAt = new Map<string, number>()
const STATUS_RETRY_MS = 60_000

/** Ask `connectionId` which hub gateway it is (cached STATUS_TTL_MS; a miss is not re-asked for STATUS_RETRY_MS;
 *  single-flight per connection). True when it answered — which also proves the gateway is reachable now. */
export async function learnHubGatewayName(connectionId: string): Promise<boolean> {
  const at = statusChecked.get(connectionId)

  if (at && Date.now() - at < STATUS_TTL_MS) {
    return true
  }

  const failedAt = statusFailedAt.get(connectionId)

  if (failedAt !== undefined && Date.now() - failedAt < STATUS_RETRY_MS) {
    return false
  }

  let pending = statusInFlight.get(connectionId)

  if (!pending) {
    pending = hubGet<{ gateway?: string }>('/status', connectionId)
      .then(status => {
        if (status?.gateway) {
          statusChecked.set(connectionId, Date.now())
          statusFailedAt.delete(connectionId)

          return true
        }

        statusFailedAt.set(connectionId, Date.now())

        return false
      })
      .finally(() => statusInFlight.delete(connectionId))
    statusInFlight.set(connectionId, pending)
  }

  return pending
}

/** Queue an owner request on the primary gateway (works with the hub down). */
export async function hubRequest(body: HubRequestBody): Promise<HubRequestResult> {
  return pluginRest<HubRequestResult>(HUB_SYNC_PLUGIN, '/request', {
    body,
    connectionId: hubReplicaConnectionId(),
    method: 'POST',
    timeoutMs: 10_000
  })
}

// ── session → owner ─────────────────────────────────────────────────────────

const whereCache = new Map<string, { at: number; hits: HubWhereHit[] }>()
const whereInFlight = new Map<string, Promise<HubWhereHit[]>>()

async function whereHits(id: string): Promise<HubWhereHit[]> {
  const cached = whereCache.get(id)

  if (cached && Date.now() - cached.at < WHERE_TTL_MS) {
    return cached.hits
  }

  let pending = whereInFlight.get(id)

  if (!pending) {
    pending = hubGet<{ hits?: HubWhereHit[] }>(`/where/${encodeURIComponent(id)}`)
      .then(result => {
        const hits = Array.isArray(result?.hits) ? result.hits : []
        whereCache.set(id, { at: Date.now(), hits })

        return hits
      })
      .finally(() => whereInFlight.delete(id))
    whereInFlight.set(id, pending)
  }

  return pending
}

/**
 * The owner of a stored session id according to the hub replica, as a
 * registry route, skipping `excluded` connections. Undefined when the replica
 * doesn't know the id, names a gateway this Desktop has no connection for, or
 * hub-sync isn't there. Live (non-archived) rows win over archived copies.
 */
export async function hubSessionOwner(
  storedSessionId: string,
  excluded: Iterable<string> = []
): Promise<SessionOwnerRoute | undefined> {
  const id = storedSessionId.trim()

  if (!id) {
    return undefined
  }

  const skip = new Set([...excluded].map(connection => connection.trim()))
  const hits = [...(await whereHits(id))].sort((a, b) => Number(Boolean(a.archived)) - Number(Boolean(b.archived)))
  const replica = hubReplicaConnectionId()

  for (const hit of hits) {
    const connectionId = hit.local ? replica : connectionForHubGateway(hit.gateway)

    if (connectionId && !skip.has(connectionId)) {
      return { connectionId, profile: hit.profile?.trim() || 'default' }
    }
  }

  return undefined
}

/** Kanban cards the replica holds for hub gateway `gateway` on `board`. */
export async function hubGatewayTasks(
  gateway: string,
  board: string,
  includeClosed = false
): Promise<HubTask[] | null> {
  const params = new URLSearchParams({ board: board || 'default', gateway })

  if (includeClosed) {
    params.set('all', '1')
  }

  const result = await hubGet<{ tasks?: HubTask[] }>(`/tasks?${params.toString()}`)

  return Array.isArray(result?.tasks) ? result.tasks : null
}

/** One gateway's member of a replica project group: what a shared-field set targets. */
export interface HubProjectMember {
  gateway: string
  profile: string
  id: string
  key: string
  kind: 'auto' | 'explicit' | 'none'
}

export interface HubProjectNode extends SidebarProjectTree {
  members?: HubProjectMember[]
}

/** Hub gateway `gateway`'s project tree as the replica holds it (hermes-hub >= 1.9.0), or null. */
export async function hubGatewayProjects(gateway: string, previewLimit = 3): Promise<HubProjectNode[] | null> {
  const params = new URLSearchParams({ gateway, preview_limit: String(previewLimit) })

  const result = await hubGet<{ gateways?: Record<string, { projects?: HubProjectNode[] }> }>(
    `/projects?${params.toString()}`
  )

  const projects = result?.gateways?.[gateway]?.projects

  return Array.isArray(projects) ? projects : null
}

/** Test seam: forget every cache (names, status, where). */
export function _resetHubSyncForTests(): void {
  memoryNames = {}

  try {
    globalThis.localStorage?.removeItem(HUB_GATEWAY_NAMES_KEY)
  } catch {
    // no storage in this environment
  }

  statusChecked.clear()
  statusInFlight.clear()
  statusFailedAt.clear()
  whereCache.clear()
  whereInFlight.clear()
}
