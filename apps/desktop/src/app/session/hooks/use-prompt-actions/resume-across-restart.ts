import { isGatewayDrainError } from '@/api/gateway-drain'
import { isReconnectableSessionWriteError, withSessionWriteReconnect } from '@/api/session-write-reconnect'
import { holdRestartingSessionResume } from '@/store/session-resume-restart'

/** Only resume is replayable here. Never wrap prompt.submit: a restart-wait
 * owner may already have durably queued that prompt. Pool/boot code owns the
 * redial; each call of run enters its existing owner-pinned recovery route. */
export async function resumeAcrossGatewayRestart<T>(key: string, run: () => Promise<T>): Promise<T> {
  let release: (() => void) | undefined
  let draining = false

  try {
    return await withSessionWriteReconnect(async () => {
      try { return await run() } catch (error) {
        draining ||= isGatewayDrainError(error)

        if (retryableResume(error, draining)) { release ??= holdRestartingSessionResume(key) }
        throw error
      }
    }, { label: `resume-restart ${key}`, budgetMs: 600_000,
      retryable: error => retryableResume(error, draining) })
  } finally { release?.() }
}

function retryableResume(error: unknown, draining: boolean): boolean {
  const message = error instanceof Error ? error.message : String(error)

  if ((error as { name?: unknown })?.name === 'AbortError') { return false }

  return isGatewayDrainError(error) || isReconnectableSessionWriteError(error) ||
    /Hermes gateway (?:is not connected|connection closed|connection changed|unavailable|protocol readiness timed out)|Backend .* is reconnecting; retry after it settles|Timed out waiting for the Hermes gateway to reconnect/i.test(message) ||
    (draining && /request timed out/i.test(message))
}
