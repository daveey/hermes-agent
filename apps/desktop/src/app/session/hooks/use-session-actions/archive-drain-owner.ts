import { isGatewayDrainError } from '@/api/gateway-drain'
import { isReconnectableSessionWriteError, withSessionWriteReconnect } from '@/api/session-write-reconnect'
import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n/types'
import type { ArchiveTrace } from '@/lib/archive-trace'
import { $connectionsRegistry } from '@/store/connection-registry-state'

import { listArchiveOwner } from './archive-owner-list'
import { archiveQueueLifetime } from './archive-queue-lifetime'
import { captureArchiveOwner } from './archive-soft-hide'
import type { findListedSession } from './utils'

/** No owner is guessed while the unified list's projection is unavailable.
 * Probe every captured registry connection directly before choosing a row. */
export async function captureArchiveOwnerAfterDrain(
  id: string,
  selectedRow: SessionInfo | undefined,
  listed: ReturnType<typeof findListedSession>,
  copy: Translations['desktop'],
  trace?: ArchiveTrace
): Promise<SessionInfo> {
  try {
    return captureArchiveOwner(id, selectedRow, listed)
  } catch (error) {
    if (
      selectedRow ||
      !(error instanceof Error) ||
      !/Session owner could not be resolved; select its row/.test(error.message)
    ) {
      throw error
    }
    const registry = $connectionsRegistry.get()

    if (!registry?.primary || !registry.connections.length) {
      throw error
    }
    const connections = registry.connections.map(connection => connection.id)
    const queue = archiveQueueLifetime(trace?.attemptId ?? id, id, copy)
    let draining = false

    try {
      return await withSessionWriteReconnect(
        async () => {
          try {
            const proved = await listArchiveOwner(id, connections, queue.run, error)

            // A topology edit invalidates the scope of the proof. Never add a
            // newly selected source to a previously captured pending intent.
            if (
              JSON.stringify($connectionsRegistry.get()?.connections.map(connection => connection.id)) !==
              JSON.stringify(connections)
            ) {
              throw error
            }

            return captureArchiveOwner(id, proved, listed)
          } catch (failure) {
            if (isGatewayDrainError(failure)) {
              draining = true

              if (await queue.enqueue()) {
                trace?.('owner=waiting-projection')
              }
            }

            throw failure
          }
        },
        {
          label: `archive-owner ${id}`,
          budgetMs: Infinity,
          sleep: queue.sleep,
          retryable: failure => draining && (isGatewayDrainError(failure) || isReconnectableSessionWriteError(failure))
        }
      )
    } finally {
      queue.dispose()
    }
  }
}
