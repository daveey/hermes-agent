import { randomUuid } from '@hermes/shared'

import { isGatewayDrainError } from '@/api/gateway-drain'
import { isReconnectableSessionWriteError, NonReplayableSessionWriteError, withSessionWriteReconnect } from '@/api/session-write-reconnect'
import type { Translations } from '@/i18n/types'
import type { ArchiveTrace } from '@/lib/archive-trace'

import { archiveQueueLifetime } from './archive-queue-lifetime'

/** A maintenance refusal has not applied the mutation. Keep its captured
 * owner and retained mutation identity; never re-run selection or preflight.
 * Subsequent transport loss belongs to that same restart. Ordinary authority
 * timeouts/conflicts still go to the existing explicit Retry UI. */
export async function archiveDrainWrite<T>(id: string, copy: Translations['desktop'], run: () => Promise<T>, trace?: ArchiveTrace): Promise<T> {
  const queue = archiveQueueLifetime(trace?.attemptId ?? randomUuid(), id, copy)
  let draining = false

  try {
    return await withSessionWriteReconnect(async () => {
      try {
        return await queue.run(run)
      } catch (error) {
        if (isGatewayDrainError(error) && !(error instanceof NonReplayableSessionWriteError)) {
          draining = true

          if (await queue.enqueue()) {trace?.('patch=waiting-restart')}
        }

        throw error
      }
    }, { label: `archive-drain ${id}`, budgetMs: Infinity, sleep: queue.sleep,
      retryable: error => draining && !(error instanceof NonReplayableSessionWriteError) &&
        (isGatewayDrainError(error) || isReconnectableSessionWriteError(error)) })
  } finally {
    queue.dispose()
  }
}
