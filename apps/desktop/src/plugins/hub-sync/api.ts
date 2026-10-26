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
  recordSessionGatewayUnavailable,
  sessionOwnerIsFresh
} from '@hermes/plugin-sdk'

import { type HubSessionRow, replicaSessionsToShow } from './model'

const REPLICA_LIMIT = 300

/** Registered non-primary connections that answer hub-sync `/status` now. */
export async function reachableConnections(): Promise<Set<string>> {
  const primary = hubReplicaConnectionId()

  const ids = host.state.gateways
    .get()
    .map(c => c.id)
    .filter(id => id && id !== primary)

  // Gateway-name discovery caches success for minutes; it is not a liveness
  // probe. Read status now so a retained name cannot keep old rows running.
  const answers = await Promise.all(
    ids.map(async id => ((await hubGet<{ gateway?: string }>('/status', id))?.gateway ? id : null))
  )

  return new Set(answers.filter((id): id is string => Boolean(id)))
}

/**
 * Status failure invalidates retained lists. Status success cannot repair a
 * failed owner list: only its next successful sidebar refresh can do that.
 */
export async function loadReplicaSidebarRows(shouldPublish: () => boolean = () => true): Promise<HubSessionRow[]> {
  const primary = hubReplicaConnectionId()

  const [result, reachable] = await Promise.all([
    hubGet<{ sessions?: HubSessionRow[] }>(`/sessions?limit=${REPLICA_LIMIT}`, primary),
    reachableConnections()
  ])

  if (!shouldPublish()) {
    return []
  }

  const rows = Array.isArray(result?.sessions) ? result.sessions : []

  for (const connection of host.state.gateways.get()) {
    if (connection.id !== primary && !reachable.has(connection.id)) {
      recordSessionGatewayUnavailable(connection.id)
    }
  }

  const primaryName = hubGatewayForConnection(primary)

  const isGatewayLive = (gateway: string, profile?: string) => {
    if (gateway === primaryName) {
      return true
    }

    const connectionId = connectionForHubGateway(gateway)

    return Boolean(connectionId && reachable.has(connectionId) && sessionOwnerIsFresh(connectionId, profile))
  }

  return replicaSessionsToShow(
    rows.filter(row => !isGatewayLive(row.gateway, row.profile)),
    [],
    () => false
  )
}
