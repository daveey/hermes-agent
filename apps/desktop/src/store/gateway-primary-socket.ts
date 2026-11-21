import { activeGatewayConnectionId, isActivePrimary, pinnedPrimaryGatewayConnectionId } from './gateway'

/**
 * [daveey] The registry connection the PRIMARY socket itself serves, whichever route is foregrounded: the tag for
 * events read off that socket. `activeGatewayConnectionId()` names the foreground source, so with a secondary
 * (e.g. hermes-hub) active it stamped the primary's own plugin pushes as the hub's. Before the primary's route is
 * recorded, Electron's active descriptor names it only while the primary is foregrounded; otherwise null (untagged).
 */
export function primarySocketConnectionId(): null | string {
  return pinnedPrimaryGatewayConnectionId() ?? (isActivePrimary() ? activeGatewayConnectionId() : null)
}
