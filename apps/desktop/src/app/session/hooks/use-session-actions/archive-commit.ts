import { type ProfileScope, setSessionArchived } from '@/hermes'
import type { Translations } from '@/i18n/types'
import { sessionWriteRetryAction } from '@/lib/session-write-retry'
import { notifyError } from '@/store/notifications'

import type { softHideArchivedRow } from './archive-soft-hide'

type HiddenRow = ReturnType<typeof softHideArchivedRow>

interface ArchiveCommit {
  storedSessionId: string
  /** Resolved once (`pinSessionWriteOwner`) before the first attempt; every Retry reuses it. */
  owner: ProfileScope
  hidden: HiddenRow
  /** Hide the row again for a Retry (the failed attempt put it back). */
  rehide: () => HiddenRow
  copy: Translations['desktop']
  onArchived: () => void
  /** The toast button's label (`t.common.retry`). */
  retryLabel: string
}

/**
 * [daveey] The archive write, after preflight. A failure restores the row and offers a Retry
 * bound to this exact owner and id: it re-hides the row and sends the same intent, so the
 * mutation client replays its retained identity (no fresh request_id for a write the owner may
 * already hold). Retry skips the preflight and the deselect, which already ran.
 */
export async function commitArchive(commit: ArchiveCommit): Promise<boolean> {
  const { copy, hidden, owner, storedSessionId } = commit

  try {
    await setSessionArchived(storedSessionId, true, owner)
    commit.onArchived()
  } catch (err) {
    hidden.restore()
    notifyError(err, copy.archiveFailed, {
      action: sessionWriteRetryAction(err, commit.retryLabel, () => void commitArchive({ ...commit, hidden: commit.rehide() })),
      id: `archive-failed:${storedSessionId}`
    })

    return false
  }

  hidden.end()

  return true
}
