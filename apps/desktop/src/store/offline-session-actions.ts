/**
 * [daveey] Owner actions on a cached (offline) sidebar row. The owning gateway
 * is unreachable, so archive / rename go to the window gateway's hub-sync as an
 * owner request (queued, applied when the owner is back); the row says so. Open
 * is refused honestly instead of dialing a dead gateway.
 */
import type { SessionInfo } from '@/hermes'
import { syncedAgoLabel } from '@/lib/synced-ago'

import { hubRequest, type HubRequestResult } from './hub-sync'
import { notify, notifyError } from './notifications'
import { findOfflineSession, hideOfflineSession } from './offline-sessions'

export type OfflineSessionAction = 'archive' | 'rename'

/** Plain-language reason a cached row cannot be opened. */
export function offlineOpenNotice(session: Pick<SessionInfo, 'offline_gateway' | 'offline_synced_at'>): string {
  const gateway = session.offline_gateway || 'its gateway'

  return `${gateway} is offline, so this chat can't be opened right now (cached, ${syncedAgoLabel(session.offline_synced_at)}). Archive and rename are queued and sent when it is back.`
}

export function offlineActionRequestBody(row: SessionInfo, action: OfflineSessionAction, args?: Record<string, unknown>) {
  const profile = (row.profile ?? '').trim() || 'default'

  return {
    action,
    ...(args ? { args } : {}),
    base_version: row.offline_version ?? null,
    coll: 'sessions' as const,
    gateway: row.offline_gateway ?? '',
    key: `${profile}/${row.id}`
  }
}

/** Queue `action` for the row's owner; archive also hides the row locally. */
export async function queueOfflineSessionAction(
  row: SessionInfo,
  action: OfflineSessionAction,
  args?: Record<string, unknown>
): Promise<HubRequestResult> {
  const result = await hubRequest(offlineActionRequestBody(row, action, args))

  if (action === 'archive') {
    hideOfflineSession(row)
  }

  return result
}

/**
 * Archive of a cached row: queue it for the owner through the hub instead of
 * dialing a dead gateway. Returns true when the id was a cached row (handled).
 */
export async function archiveIfOfflineCached(
  storedSessionId: string,
  copy: { archiveFailed: string; archived: string }
): Promise<boolean> {
  const row = findOfflineSession(storedSessionId)

  if (!row) {
    return false
  }

  try {
    await queueOfflineSessionAction(row, 'archive')
    notify({ durationMs: 4_000, kind: 'success', message: copy.archived })
  } catch (err) {
    notifyError(err, copy.archiveFailed)
  }

  return true
}
