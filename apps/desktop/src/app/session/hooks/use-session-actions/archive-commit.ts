import { hermesApiAs } from '@/api/client'
import { type ProfileScope, setSessionArchived } from '@/hermes'
import type { Translations } from '@/i18n/types'
import { type ArchiveTrace, startArchiveTrace } from '@/lib/archive-trace'
import { sessionWriteRetryAction } from '@/lib/session-write-retry'
import { notifyError } from '@/store/notifications'

import { archiveDrainWrite } from './archive-drain-write'
import type { softHideArchivedRow } from './archive-soft-hide'

type HiddenRow = ReturnType<typeof softHideArchivedRow>

interface ArchiveCommit {
  trace?: ArchiveTrace
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
    let sends = 0

    await archiveDrainWrite(
      storedSessionId,
      copy,
      () =>
        commit.trace
          ? setSessionArchived(storedSessionId, true, owner, () => commit.trace?.(`patch-sent n=${++sends}`))
          : setSessionArchived(storedSessionId, true, owner),
      commit.trace
    )

    // A write ACK alone is not archived=true evidence. Use the same pinned
    // owner and the real IPC read path. Failed verification never replays a
    // committed mutation or pretends its optimistic DOM is backend truth.
    // Diagnostic-only: don't hold success/optimistic-hide release behind a slow GET.
    if (commit.trace) {
      const scope = owner && typeof owner === 'object' ? owner : { profile: owner }
      void Promise.resolve()
        .then(() =>
          hermesApiAs<{ id: string; archived?: boolean | number }>(
            { connectionId: scope.connectionId ?? null, profile: scope.profile ?? null },
            { path: `/api/sessions/${encodeURIComponent(storedSessionId)}`, timeoutMs: 6_000 }
          )
        )
        .then(row =>
          commit.trace?.(
            row.id === storedSessionId && (row.archived === true || row.archived === 1)
              ? 'committed'
              : 'verification-pending:not-archived'
          )
        )
        .catch(() => commit.trace?.('verification-pending:read-failed'))
    }

    commit.onArchived()
  } catch (err) {
    commit.trace?.(`failed:${String(err)}`)
    hidden.restore()
    notifyError(err, copy.archiveFailed, {
      action: sessionWriteRetryAction(
        err,
        commit.retryLabel,
        () =>
          void commitArchive({
            ...commit,
            trace: commit.trace ? startArchiveTrace(storedSessionId) : undefined,
            hidden: commit.rehide()
          })
      ),
      id: `archive-failed:${storedSessionId}`
    })

    return false
  }

  hidden.end()

  return true
}
