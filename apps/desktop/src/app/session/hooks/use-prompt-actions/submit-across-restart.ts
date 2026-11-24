import { ambientOwnerConnectionId } from '@/api/client'
import { isGatewayDrainError } from '@/api/gateway-drain'
import { isReconnectableSessionWriteError, withSessionWriteReconnect } from '@/api/session-write-reconnect'
import type { SessionOwnerScope } from '@/store/session-request-router'
import { holdRestartingSessionResume } from '@/store/session-resume-restart'

import { SessionRecoveryAborted } from './utils'

/** ONLY identified submissions may enter this boundary. A lost ACK is replayed
 * with the exact saved submission_id/payload: the owner returns its receipt,
 * including restart-wait's queued/unknown verdict, never a second admission.
 * An identityless legacy send must never enter this function. */
export async function submitAcrossGatewayRestart<T>(options: {
  storedSessionId: string | null
  owner: SessionOwnerScope
  driftReason: () => string | null
  onUncertain: () => void
}, run: () => Promise<T>): Promise<T> {
  let release: (() => void) | undefined
  const connectionId = typeof options.owner === 'object' && options.owner
    ? options.owner.connectionId : ambientOwnerConnectionId()
  const key = JSON.stringify([connectionId || '', options.storedSessionId])

  try {
    return await withSessionWriteReconnect(async () => {
      const drift = options.driftReason()
      if (drift) { throw new SessionRecoveryAborted(drift, '') }

      try { return await run() } catch (error) {
        if (retryable(error)) {
          options.onUncertain()
          release ??= holdRestartingSessionResume(key)
        }
        throw error
      }
    }, { label: `submit-restart ${options.storedSessionId}`, budgetMs: 600_000, retryable })
  } finally { release?.() }
}

function retryable(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return isGatewayDrainError(error) || isReconnectableSessionWriteError(error) ||

    /Hermes gateway (?:is not connected|connection closed|unavailable)|Backend .* is reconnecting; retry after it settles|Timed out waiting for the Hermes gateway to reconnect/i.test(message)
}
