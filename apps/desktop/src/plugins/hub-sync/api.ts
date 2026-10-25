/**
 * [daveey] Data side of the hub-sync fallback: replica session records of
 * gateways that are not live right now (the ordinary sidebar filters them). Best-effort throughout: with hub-sync/hermes-hub missing or the hub
 * down every read answers [] and the live sidebar is untouched.
 */
import {
  connectionForHubGateway,
  host,
  hubGatewayForConnection,
  hubGet,
  hubReplicaConnectionId,
  learnHubGatewayName
} from '@hermes/plugin-sdk'

import { type HubSessionRow, type LiveSessionKey, replicaSessionsToShow } from './model'

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
