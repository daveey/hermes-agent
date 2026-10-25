/** hermes-hub replica access (hub-sync): replica reads, gateway-name mapping and
 *  owner requests. Best-effort: every read answers null/[] when hub-sync or the
 *  hub is missing, so callers must keep their live path authoritative. */
export {
  connectionForHubGateway,
  hubGatewayForConnection,
  hubGatewayTasks,
  hubGet,
  hubReplicaConnectionId,
  hubRequest,
  learnHubGatewayName
} from '@/store/hub-sync'
import { connectionForHubGateway } from '@/store/hub-sync'
import {
  $offlineSessions,
  type OfflineReplicaRecord,
  offlineSessionsFromReplica,
  withoutLocallyArchived
} from '@/store/offline-sessions'
export type { HubPending, HubRecord, HubRequestBody, HubRequestResult, HubTask } from '@/store/hub-sync'

export type { OfflineReplicaRecord } from '@/store/offline-sessions'

/** Hand the ordinary sidebar the cached rows of unreachable gateways (fallback
 *  inputs: it filters, groups and sorts them like live rows). `[]` clears. */
export function publishOfflineSessions(records: readonly OfflineReplicaRecord[]): void {
  $offlineSessions.set(withoutLocallyArchived(offlineSessionsFromReplica(records, connectionForHubGateway)))
}
