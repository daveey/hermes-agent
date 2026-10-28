import type { SessionInfo } from '@/hermes'
import { getSessionOwnerHint } from '@/store/session'

import { cachedSessionRowCandidates } from './utils'

/**
 * The row whose owner an archive without a selected row (the `session.archive`
 * hotkey, a tile's tab menu, plugins) checks and archives against.
 *
 * Those doors only had Recents ($sessions, plus offline copies) to go on. While
 * a remote gateway's pooled backend reconnects, the unified list carries none
 * of its rows, so an open remote chat had no row there: the linked-work
 * preflight then read the registry primary, which 404s (silently) for a remote
 * session, and the dialog showed both "Task status unavailable" and "Session
 * ancestry unavailable" for a chat with nothing unknown about it.
 *
 * Resolve the owner the way resume/branch already do: a connection-tagged row
 * from any cached list (project tree, cron, messaging) beats an ownerless
 * Recents copy, and with no row at all the single remembered owner hint names
 * the gateway. An untagged row with no tagged twin keeps meaning "primary".
 */
export function archiveOwnerRow(storedSessionId: string, row: SessionInfo | undefined): SessionInfo | undefined {
  if (row?.connection_id?.trim() || row?.offline_cached) {
    return row
  }

  const tagged = cachedSessionRowCandidates(storedSessionId).find(candidate => candidate.connection_id?.trim())

  if (tagged) {
    return row
      ? { ...row, connection_id: tagged.connection_id, profile: tagged.profile?.trim() || row.profile }
      : tagged
  }

  if (row) {
    return row
  }

  const hint = getSessionOwnerHint(storedSessionId)

  return hint?.connectionId
    ? ({ connection_id: hint.connectionId, id: storedSessionId, profile: hint.profile } as SessionInfo)
    : undefined
}
