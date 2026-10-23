/**
 * [daveey] Data side of the hub-sync sidebar section: replica session rows for
 * gateways that are not live right now, and the owner requests their actions
 * send. Best-effort throughout: with hub-sync/hermes-hub missing or the hub
 * down every read answers [] and the live sidebar is untouched.
 */
import {
  connectionForHubGateway,
  host,
  hubGatewayForConnection,
  hubGet,
  hubReplicaConnectionId,
  hubRequest,
  type HubRequestResult,
  learnHubGatewayName
} from '@hermes/plugin-sdk'

import {
  type HubSessionAction,
  type HubSessionRow,
  type LiveSessionKey,
  replicaSessionsToShow,
  sessionRequestBody
} from './model'

const REPLICA_LIMIT = 300

interface LiveRow {
  connection_id?: string
  id: string
}

/** Hub gateway name of a live row (untagged rows belong to the primary). */
function liveRowGateway(row: LiveRow, primary: string): string {
  const connectionId = row.connection_id?.trim() || primary

  return hubGatewayForConnection(connectionId) ?? connectionId
}

export function liveSessionKeys(live: readonly LiveRow[]): LiveSessionKey[] {
  const primary = hubReplicaConnectionId()

  return live.filter(row => row?.id).map(row => ({ gateway: liveRowGateway(row, primary), id: row.id }))
}

/** Registered non-primary connections that answer hub-sync `/status` now. */
export async function reachableConnections(): Promise<Set<string>> {
  const primary = hubReplicaConnectionId()
  const ids = host.state.gateways.get().map(c => c.id).filter(id => id && id !== primary)
  const answers = await Promise.all(ids.map(async id => ((await learnHubGatewayName(id)) ? id : null)))

  return new Set(answers.filter((id): id is string => Boolean(id)))
}

/**
 * Replica rows the sidebar should add: gateways that neither answered a
 * status probe nor contributed rows to the live list (unreachable or not
 * registered on this Desktop), deduped against the live list by (gateway, id).
 */
export async function loadReplicaSidebarRows(live: readonly LiveRow[]): Promise<HubSessionRow[]> {
  const primary = hubReplicaConnectionId()

  const [result, reachable] = await Promise.all([
    hubGet<{ sessions?: HubSessionRow[] }>(`/sessions?limit=${REPLICA_LIMIT}`, primary),
    reachableConnections()
  ])

  const rows = Array.isArray(result?.sessions) ? result.sessions : []

  if (!rows.length) {
    return []
  }

  const liveConnections = new Set(live.map(row => row.connection_id?.trim() || primary))
  const primaryName = hubGatewayForConnection(primary)

  const isGatewayLive = (gateway: string) => {
    if (gateway === primaryName) {
      return true
    }

    const connectionId = connectionForHubGateway(gateway)

    return Boolean(connectionId && (reachable.has(connectionId) || liveConnections.has(connectionId)))
  }

  return replicaSessionsToShow(rows, liveSessionKeys(live), isGatewayLive)
}

/** Queue `action` on the row's owning gateway (offline-safe on the primary). */
export function sendSessionAction(
  row: HubSessionRow,
  action: HubSessionAction,
  args?: Record<string, unknown>
): Promise<HubRequestResult> {
  return hubRequest(sessionRequestBody(row, action, args))
}

/** Owner route for opening a replica row; null when this Desktop has no
 *  connection for the row's gateway. */
export function replicaRowRoute(row: HubSessionRow) {
  const connectionId = connectionForHubGateway(row.gateway)

  if (!connectionId) {
    return null
  }

  const profile = row.profile?.trim() || 'default'
  const kind = host.state.gateways.get().find(c => c.id === connectionId)?.kind
  const mode = kind === 'local' ? ('local' as const) : ('remote' as const)

  return { connectionId, mode, profile, targetProfile: profile }
}
