import { randomUuid } from '@hermes/shared'

import { isReconnectableSessionWriteError, withSessionWriteReconnect } from '@/api/session-write-reconnect'
import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n/types'
import type { ArchiveTrace } from '@/lib/archive-trace'
import { $connectionsRegistry } from '@/store/connection-registry-state'

import { archiveQueueLifetime } from './archive-queue-lifetime'
import { refreshArchiveTasks } from './refresh-archive-tasks'

export { reportDroppedArchiveQueues } from './archive-queue-lifetime'

export async function reconnectArchivePreflight(id: string, row: SessionInfo | undefined, profile: string | undefined,
  copy: Translations['desktop'], trace?: ArchiveTrace): Promise<SessionInfo | undefined> {
  // Pin even an ownerless primary row before waiting: a gateway switch cannot
  // move its safety read to a different same-named profile.
  const session = { ...row, connection_id: row?.connection_id || $connectionsRegistry.get()?.primary || undefined } as SessionInfo
  const queue = archiveQueueLifetime(trace?.attemptId ?? randomUuid(), id, copy)
  let queued = false

  try {
    return await withSessionWriteReconnect(async () => {
      try {
        return await queue.run(() => refreshArchiveTasks(id, session, profile,
          { retryTransport: true, forceRefresh: queued, signal: queue.signal, trace }))
      } catch (error) {
        if (isReconnectableSessionWriteError(error)) {
          trace?.('preflight=waiting-reconnect')
          queued = true

          if (await queue.enqueue()) {trace?.('queued')}
        }

        throw error
      }
    // Intent lifetime and individual safety-read deadlines are different:
    // back off indefinitely during transport loss, but bound every full read.
    }, { label: `archive-preflight ${id}`, budgetMs: Infinity, sleep: queue.sleep })
  } finally {
    queue.dispose()
  }
}
