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

async function markUnreachableGateways(primary: string): Promise<void> {
  try {
    const reachable = await reachableConnections()

    for (const connection of host.state.gateways.get()) {
      if (connection.id !== primary && !reachable.has(connection.id)) {
        recordSessionGatewayUnavailable(connection.id)
      }
    }
  } catch {
    // best effort
  }
}

/** The replica's pull cursor: it moves only when new hub data arrived, so polling it is cheap. */
export async function replicaCursor(): Promise<null | number> {
  const st = await hubGet<{ cursor?: number }>('/status', hubReplicaConnectionId())

  return typeof st?.cursor === 'number' ? st.cursor : null
}

/**
 * Status failure invalidates retained lists. Status success cannot repair a
 * failed owner list: only its next successful sidebar refresh can do that.
 *
 * [daveey] null = no answer (backend still starting, hub-sync unavailable): publish nothing, so a failed read never
 * wipes rows already showing (the cold-start boot snapshot's, or the last good poll's).
 */
export async function loadReplicaSidebarRows(
  shouldPublish: () => boolean = () => true
): Promise<HubSessionRow[] | null> {
  const primary = hubReplicaConnectionId()

  // The replica read is local and answers in milliseconds; never wait on the reachability probes (each one goes over
  // SSH to its gateway). They only invalidate retained live lists, in the background.
  void markUnreachableGateways(primary)
  const result = await hubGet<{ sessions?: HubSessionRow[] }>(`/sessions?limit=${REPLICA_LIMIT}`, primary)

  if (!shouldPublish() || !result || !Array.isArray(result.sessions)) {
    return null
  }

  const rows = result.sessions
  const primaryName = hubGatewayForConnection(primary)

  // A live owner list wins over the replica only once it is fresh; until then (a cold start) the replica rows show.
  const isGatewayLive = (gateway: string, profile?: string) => {
    if (gateway === primaryName) {
      return true
    }

    const connectionId = connectionForHubGateway(gateway)

    return Boolean(connectionId && sessionOwnerIsFresh(connectionId, profile))
  }

  return replicaSessionsToShow(
    rows.filter(row => !isGatewayLive(row.gateway, row.profile)),
    [],
    () => false
  )
}
