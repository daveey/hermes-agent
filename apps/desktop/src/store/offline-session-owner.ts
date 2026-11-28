import type { SessionInfo } from '@/hermes'

import { $sidebarFallbackSessions } from './boot-snapshot'
import { mergeOfflineFallback, offlineIdentity, projectTreeSessionRows } from './offline-sessions'
import { $projectTree } from './project-tree'
import { $sessions, sessionMatchesStoredId } from './session'

/** Resolve the clicked owner, never the first cached row sharing a bare id.
 * Use the sidebar's effective rows so reconnect wins even before the cache poll.
 */
export function sessionActionRow(storedSessionId: string, selectedRow?: SessionInfo): SessionInfo | undefined {
  const effective = mergeOfflineFallback(
    $sessions.get(),
    $sidebarFallbackSessions.get(),
    undefined,
    projectTreeSessionRows($projectTree.get())
  )

  const matches = effective.filter(row => sessionMatchesStoredId(row, storedSessionId))

  if (selectedRow) {
    if (selectedRow.offline_cached) {
      requireOfflineOwner(selectedRow)
    }

    if (!sessionMatchesStoredId(selectedRow, storedSessionId)) {
      throw new Error('Session owner does not match the selected row')
    }

    const current = matches.find(
      row =>
        (row.connection_id?.trim() || '') === (selectedRow.connection_id?.trim() || '') &&
        (row.profile?.trim() || 'default') === (selectedRow.profile?.trim() || 'default')
    )

    // Resolve the same owner against current freshness, even if the menu
    // opened before its last list failed. Never select a different owner.
    if (!selectedRow.offline_cached) {
      return current ?? selectedRow
    }

    if (!current) {
      throw new Error('Selected session is no longer available offline')
    }

    return current
  }

  const live = matches.filter(row => !row.offline_cached)

  if (new Set(live.map(offlineIdentity)).size > 1 || (!live.length && matches.length)) {
    throw new Error('Session owner could not be resolved; select its row')
  }

  return live[0]
}

export function requireOfflineOwner(row: SessionInfo): void {
  if (!row.offline_cached || !row.connection_id?.trim() || !row.profile?.trim() || !row.offline_gateway?.trim()) {
    throw new Error('Offline session owner could not be resolved')
  }
}
