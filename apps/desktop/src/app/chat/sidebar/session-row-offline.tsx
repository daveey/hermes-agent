/**
 * [daveey] The "cached from an unreachable gateway" mark on a sidebar row:
 * a disconnect glyph with the sync age, never a live indicator.
 */
import type { ReactNode } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { syncedAgoLabel } from '@/lib/synced-ago'
import type { $connectionsRegistry } from '@/store/connection-registry-state'

import { GatewayDeviceIcon } from './gateway-device-icon'

/**
 * Trailing gateway glyphs of a row: the device icon once several gateways are
 * registered, plus the offline mark when the row is a cached one.
 */
export function gatewayMarks(
  session: SessionInfo,
  registry: ReturnType<typeof $connectionsRegistry.get>,
  gatewayId: string
): ReactNode {
  const gatewayIcon =
    (registry?.connections.length ?? 0) > 1 ? (
      <GatewayDeviceIcon className="ml-1 align-[-2px]" gatewayId={gatewayId} />
    ) : null

  if (!session.offline_cached) {
    return gatewayIcon
  }

  const label = `Offline · cached from ${session.offline_gateway ?? 'its gateway'}, ${syncedAgoLabel(session.offline_synced_at)}`

  return (
    <>
      {gatewayIcon}
      <Tip label={label} side="top">
        <span
          aria-label={label}
          className="pointer-events-auto ml-1 inline-flex items-center align-[-1px]"
          data-testid="session-row-offline"
          role="img"
        >
          <Codicon name="debug-disconnect" size="0.625rem" />
        </span>
      </Tip>
    </>
  )
}
