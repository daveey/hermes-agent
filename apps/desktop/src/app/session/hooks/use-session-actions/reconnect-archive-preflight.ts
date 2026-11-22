import { randomUuid } from '@hermes/shared'

import { isReconnectableSessionWriteError, withSessionWriteReconnect } from '@/api/session-write-reconnect'
import type { SessionInfo } from '@/hermes'
import { translateNow } from '@/i18n'
import type { Translations } from '@/i18n/types'
import type { ArchiveTrace } from '@/lib/archive-trace'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { dismissNotification, notify } from '@/store/notifications'

import { refreshArchiveTasks } from './refresh-archive-tasks'

const PREFIX = 'hermes:archive-preflight-queue:'
const active = new Set<string>()

/** A reload cancels the window-local operation, never resumes a stale safety verdict.
 * Keep a marker until settlement so the next renderer explains that cancellation. */
export function reportDroppedArchiveQueues(): void {
  const keys = Object.keys(sessionStorage).filter(key => key.startsWith(PREFIX) && !active.has(key))

  if (!keys.length) {return}
  keys.forEach(key => sessionStorage.removeItem(key))
  notify({ id: 'archive-queue-reload', kind: 'warning', persistent: true, durationMs: 0,
    message: translateNow('desktop.archiveQueueDropped') })
}

export async function reconnectArchivePreflight(id: string, row: SessionInfo | undefined, profile: string | undefined,
  copy: Translations['desktop'], trace?: ArchiveTrace): Promise<SessionInfo | undefined> {
  // Pin even an ownerless primary row before waiting: a gateway switch cannot
  // move its safety read to a different same-named profile.
  const session = { ...row, connection_id: row?.connection_id || $connectionsRegistry.get()?.primary || undefined } as SessionInfo
  const key = `${PREFIX}${trace?.attemptId ?? randomUuid()}`
  let queued = false
  active.add(key)

  try {
    return await withSessionWriteReconnect(async remainingMs => {
      try {
        const result = await refreshArchiveTasks(id, session, profile, { retryTransport: true, budgetMs: remainingMs, trace })

        return result
      } catch (error) {
        if (isReconnectableSessionWriteError(error)) {
          trace?.('preflight=waiting-reconnect')

          if (!queued) {
            queued = true
            sessionStorage.setItem(key, id)
            notify({ id: key, kind: 'info', persistent: true, durationMs: 0, message: copy.archiveQueued })
            trace?.('queued')
          }
        }

        throw error
      }
    }, { label: `archive-preflight ${id}` })
  } finally {
    active.delete(key)
    sessionStorage.removeItem(key)

    if (queued) {dismissNotification(key)}
  }
}
