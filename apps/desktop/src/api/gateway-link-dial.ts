// [daveey] The chat WebSocket's connect limit follows Desktop main's slow-link mode (electron/slow-link.ts).
//
// Before each dial the renderer asks main for the limit of that URL: 15 s, or up to 60 s while the SSH-forwarded
// backend behind it is on a slow link. After the dial it reports how it ended, so an open counts as an answer and a
// timeout as a strike for that link. Without a Desktop bridge (browser-hosted, tests) the client default applies.

import { GATEWAY_CONNECT_TIMEOUT_CODE } from '@hermes/shared'

export type GatewayConnectOutcome = { elapsedMs: number; kind: 'opened' } | { kind: 'timed-out' }

export interface GatewayLinkBridge {
  gatewayConnectLimit?: (wsUrl: string) => number
  noteGatewayConnect?: (wsUrl: string, outcome: GatewayConnectOutcome) => void
}

const desktopBridge = (): GatewayLinkBridge | undefined =>
  (globalThis as { hermesDesktop?: GatewayLinkBridge }).hermesDesktop

/** The limit main gives this dial, or undefined (client default) when main has no answer. */
function connectLimit(bridge: GatewayLinkBridge | undefined, wsUrl: string): number | undefined {
  try {
    const limit = bridge?.gatewayConnectLimit?.(wsUrl)

    return typeof limit === 'number' && Number.isFinite(limit) && limit > 0 ? limit : undefined
  } catch {
    return undefined
  }
}

function report(bridge: GatewayLinkBridge | undefined, wsUrl: string, outcome: GatewayConnectOutcome): void {
  try {
    bridge?.noteGatewayConnect?.(wsUrl, outcome)
  } catch {
    // Reporting is advisory: the dial's own result stands.
  }
}

/**
 * Run one WebSocket dial with the connect limit main gives `wsUrl`, then report its outcome. `dial` receives the
 * per-dial options for `JsonRpcGatewayClient.connect`; its result or rejection is passed through unchanged.
 */
export async function dialWithLinkLimit(
  wsUrl: string,
  dial: (options: { connectTimeoutMs?: number }) => Promise<void>,
  bridge: GatewayLinkBridge | undefined = desktopBridge(),
  now: () => number = Date.now
): Promise<void> {
  const connectTimeoutMs = connectLimit(bridge, wsUrl)
  const startedAt = now()

  try {
    await dial(connectTimeoutMs === undefined ? {} : { connectTimeoutMs })
  } catch (error) {
    if ((error as { code?: unknown } | null)?.code === GATEWAY_CONNECT_TIMEOUT_CODE) {
      report(bridge, wsUrl, { kind: 'timed-out' })
    }

    throw error
  }

  report(bridge, wsUrl, { elapsedMs: now() - startedAt, kind: 'opened' })
}
