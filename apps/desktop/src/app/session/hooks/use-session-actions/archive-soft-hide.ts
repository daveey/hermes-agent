import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n/types'
import { $pinnedSessionIds } from '@/store/layout'
import { notifyError } from '@/store/notifications'
import { archiveIfOfflineCached } from '@/store/offline-session-actions'
import { $offlineSessions, offlineIdentity } from '@/store/offline-sessions'
import { $profiles } from '@/store/profile'
import { sessionPinId } from '@/store/session'
import {
  beginSessionMutation,
  endSessionMutation,
  tombstoneSessions,
  untombstoneSessions
} from '@/store/session-removal'

import { confirmLinkedArchive } from './confirm-linked-archive'
import { dropListedSession, type findListedSession, resolveSessionProfile, restoreListedSession } from './utils'

/**
 * [daveey] Archive hides the row the moment the user asks, before the
 * linked-work preflight (ancestry + task reads, plugin guards: up to ~14 s on a
 * busy or remote backend) and the archive RPC. A cancelled confirmation, an
 * unresolved owner or a failed RPC puts the row back via `restore()`.
 */
export function softHideArchivedRow(
  storedSessionId: string,
  archived: SessionInfo | undefined,
  listed: ReturnType<typeof findListedSession>,
  listedRow: SessionInfo | undefined
) {
  const previousPinned = $pinnedSessionIds.get()
  // Pins are keyed on the durable lineage-root id; the stored id may be the
  // live tip after compression. Drop both so the pin can't linger.
  const pinId = archived ? sessionPinId(archived) : storedSessionId
  const ids = [storedSessionId, archived?.id, archived?._lineage_root_id]

  dropListedSession(storedSessionId)
  tombstoneSessions(ids)
  beginSessionMutation(ids)
  $pinnedSessionIds.set(previousPinned.filter(id => id !== storedSessionId && id !== pinId))

  const hidden = {
    end: () => endSessionMutation(ids),
    ids,
    /** A queued offline archive drops its cached row; a failed queue leaves it, so it comes back. */
    settleOffline: () => {
      const queued =
        !archived || !$offlineSessions.get().some(row => offlineIdentity(row) === offlineIdentity(archived))

      if (queued) {
        hidden.end()
      } else {
        hidden.restore()
      }
    },
    restore: () => {
      // Only a row the sidebar actually listed comes back; an owner resolved
      // from the project tree or a hint was never in that slice.
      if (listedRow) {
        restoreListedSession(listedRow, listed?.slice)
      }

      untombstoneSessions(ids)
      $pinnedSessionIds.set(previousPinned)
      endSessionMutation(ids)
    }
  }

  return hidden
}

type HiddenRow = ReturnType<typeof softHideArchivedRow>

/**
 * Owner resolution, linked-work confirmation and the offline-queue path, run
 * while the row is already hidden. Returns the profile to archive under, or
 * false when archive is over: cancelled/failed (row restored) or queued offline.
 */
export async function archivePreflight(
  storedSessionId: string,
  archived: SessionInfo | undefined,
  listed: boolean,
  hidden: HiddenRow,
  copy: Translations['desktop'],
  options?: { skipLinkedConfirm?: boolean }
): Promise<false | string | undefined> {
  const stampedProfile = archived?.profile?.trim()
  const profile = stampedProfile || (await resolveSessionProfile(storedSessionId))

  if (listed && !stampedProfile && !profile?.trim() && $profiles.get().filter(item => item.name.trim()).length > 1) {
    hidden.restore()
    notifyError(new Error('Session ownership could not be resolved'), copy.archiveFailed)

    return false
  }

  if (!(await confirmLinkedArchive(storedSessionId, archived, profile, copy, options?.skipLinkedConfirm))) {
    hidden.restore()

    return false
  }

  try {
    // Offline-or-live was decided before the hide dropped the live row.
    if (archived?.offline_cached && (await archiveIfOfflineCached(storedSessionId, copy, archived))) {
      hidden.settleOffline()

      return false
    }
  } catch (err) {
    hidden.restore()
    notifyError(err, copy.archiveFailed)

    return false
  }

  return profile
}
