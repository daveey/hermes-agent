import type { SessionInfo } from '@/hermes'
import type { Translations } from '@/i18n/types'
import type { ArchiveTrace } from '@/lib/archive-trace'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pinnedSessionIds } from '@/store/layout'
import { notifyError } from '@/store/notifications'
import { archiveIfOfflineCached } from '@/store/offline-session-actions'
import { sessionActionRow } from '@/store/offline-session-owner'
import { $profiles } from '@/store/profile'
import { sessionPinId } from '@/store/session'
import {
  beginSessionMutation,
  endSessionMutation,
  tombstoneSessions,
  untombstoneSessions
} from '@/store/session-removal'

import { archiveOwnerRow } from './archive-owner'
import { confirmLinkedArchive } from './confirm-linked-archive'
import { dropListedSession, type findListedSession, resolveSessionProfile, restoreListedSession } from './utils'

export function captureArchiveOwner(storedSessionId: string, selectedRow: SessionInfo | undefined, listed: ReturnType<typeof findListedSession>): SessionInfo {
  let row = sessionActionRow(storedSessionId, selectedRow) ?? listed?.session

  // Explicit selection is authoritative; other entry paths resolve like resume.
  if (!selectedRow) {row = archiveOwnerRow(storedSessionId, row)}

  return { ...row, connection_id: row?.connection_id || $connectionsRegistry.get()?.primary || undefined } as SessionInfo
}

/**
 * [daveey] Archive hides the row the moment the user asks, before the
 * linked-work preflight (ancestry + task reads, plugin guards: up to ~14 s on a
 * busy or remote backend) and the archive RPC. A cancelled confirmation, an
 * unresolved owner or a failed RPC puts the row back via `restore()`.
 *
 * Offline-cached rows are not pre-hidden (`hid` false): tombstones and the
 * live-slice drop are keyed by bare id, which would also hide another owner's
 * cached chat with the same id; their owner-scoped queue path hides them.
 */
export function softHideArchivedRow(
  storedSessionId: string,
  archived: SessionInfo | undefined,
  listed: ReturnType<typeof findListedSession>
) {
  const ids = [storedSessionId, archived?.id, archived?._lineage_root_id]

  if (archived?.offline_cached) {
    return { end: () => {}, hid: false, ids, restore: () => {} }
  }

  const before = $pinnedSessionIds.get()
  // Pins are keyed on the durable lineage-root id; the stored id may be the
  // live tip after compression. Drop both so the pin can't linger.
  const pinId = archived ? sessionPinId(archived) : storedSessionId
  const removedPins = before.flatMap((id, index) => (id === storedSessionId || id === pinId ? [{ id, index }] : []))

  dropListedSession(storedSessionId)
  tombstoneSessions(ids)
  beginSessionMutation(ids)

  if (removedPins.length) {
    $pinnedSessionIds.set(before.filter(id => id !== storedSessionId && id !== pinId))
  }

  return {
    end: () => endSessionMutation(ids),
    hid: true,
    ids,
    restore: () => {
      // Only the row a live sidebar slice actually held comes back, to that
      // slice; a row resolved from the project tree or a hint never was there.
      if (listed) {
        restoreListedSession(listed.session, listed.slice)
      }

      untombstoneSessions(ids)

      // Put back only the pins this archive removed; keep pin edits made meanwhile.
      if (removedPins.length) {
        const pins = [...$pinnedSessionIds.get()]

        for (const { id, index } of removedPins) {
          if (!pins.includes(id)) {
            pins.splice(Math.min(index, pins.length), 0, id)
          }
        }

        $pinnedSessionIds.set(pins)
      }

      endSessionMutation(ids)
    }
  }
}

type HiddenRow = ReturnType<typeof softHideArchivedRow>

/**
 * Owner resolution, linked-work confirmation and the offline-queue path, run
 * while a live row is already hidden. Returns the profile to archive under, or
 * false when archive is over: cancelled/failed (row restored) or queued offline.
 */
export async function archivePreflight(
  storedSessionId: string,
  archived: SessionInfo | undefined,
  listed: boolean,
  hidden: HiddenRow,
  copy: Translations['desktop'],
  options?: { skipLinkedConfirm?: boolean },
  trace?: ArchiveTrace
): Promise<false | string | undefined> {
  const stampedProfile = archived?.profile?.trim()
  const profile = stampedProfile || (await resolveSessionProfile(storedSessionId))

  if (listed && !stampedProfile && !profile?.trim() && $profiles.get().filter(item => item.name.trim()).length > 1) {
    hidden.restore()
    notifyError(new Error('Session ownership could not be resolved'), copy.archiveFailed)

    return false
  }

  try {
    if (!(await confirmLinkedArchive(storedSessionId, archived, profile, copy, options?.skipLinkedConfirm, trace))) {
      trace?.('failed:confirmation-cancelled')
      hidden.restore()

      return false
    }
  } catch (error) {
    trace?.(`failed:${String(error)}`)
    hidden.restore()
    notifyError(error, copy.archiveFailed)

    return false
  }

  try {
    // Offline-or-live is decided from the pre-hide row: once a live row is
    // dropped, re-resolving could land on another cached copy.
    if (archived?.offline_cached && (await archiveIfOfflineCached(storedSessionId, copy, archived))) {

      return false
    }
  } catch (err) {
    hidden.restore()
    notifyError(err, copy.archiveFailed)

    return false
  }

  return profile
}
