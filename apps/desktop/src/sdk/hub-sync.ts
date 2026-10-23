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
export type { HubPending, HubRecord, HubRequestBody, HubRequestResult, HubTask } from '@/store/hub-sync'
