/**
 * Cheap signature compares for poll loops — swap the atom (and re-render)
 * only when the rows/transcript actually changed.
 */

import type { SessionInfo, SessionMessage } from '@/hermes'

// [daveey] Which background processes a row lists: one changing for another
// keeps the count but must still repaint the badge's tooltip.
const backgroundProcessKey = (session: SessionInfo): string =>
  (session.background_processes ?? []).map(proc => proc.id).join(',')

// [daveey] The stored outcome, compared by value (a fresh parse each page).
const outcomeKey = (session: SessionInfo): string => (session.outcome ? JSON.stringify(session.outcome) : '')

// [daveey] Stamps that paint the stripe directly: a page that moves only these (a review ask, the backend park, the
// stored outcome) must land.
// Optional evidence changes the row's status without any count moving; compare by value (JSON-like, producer-owned).
const sameExecutionObservations = (a: SessionInfo, b: SessionInfo): boolean =>
  a.execution_observations === b.execution_observations ||
  JSON.stringify(a.execution_observations ?? null) === JSON.stringify(b.execution_observations ?? null)

const sameStatusStamps = (a: SessionInfo, b: SessionInfo): boolean =>
  a.review_wait === b.review_wait && a.muted === b.muted && outcomeKey(a) === outcomeKey(b)

const sameSessionWorkspace = (a: SessionInfo, b: SessionInfo): boolean =>
  a.cwd === b.cwd && a.git_repo_root === b.git_repo_root && a.git_branch === b.git_branch

export function sameCronSignature(a: SessionInfo[], b: SessionInfo[]): boolean {
  if (a.length !== b.length) {
    return false
  }

  return a.every((session, i) => {
    const other = b[i]

    return (
      other != null &&
      session.id === other.id &&
      session._lineage_root_id === other._lineage_root_id &&
      session.title === other.title &&
      session.source === other.source &&
      session.profile === other.profile &&
      session.preview === other.preview &&
      session.message_count === other.message_count &&
      session.last_active === other.last_active &&
      session.ended_at === other.ended_at &&
      // A workspace move need not change activity or message counts. Let its
      // authoritative row reach the project overlays and move-target menu.
      sameSessionWorkspace(session, other) &&
      // Row STATE, not just row content: session-pin-sync reconciles the
      // sidebar's pins against `pinned` on the rows in this atom, so a page
      // whose only delta is a flag has to swap in or the reconciler reads a
      // frozen copy forever. An idle conversation never moves any of the
      // fields above again, which is exactly when a pin gets toggled (#76919).
      session.pinned === other.pinned &&
      session.archived === other.archived &&
      // Live activity the backend stamps on each row. A kanban card finishing
      // or a background job exiting moves only these, and they drive the
      // sidebar badge and the done/working stripe. [daveey]
      session.kanban_active === other.kanban_active &&
      // `needs_you` drives the red stripe on its own (authoritative when stamped), so its arrival or removal
      // must publish even when the counts did not move.
      JSON.stringify([session.kanban_blocked, session.kanban_blocked_on, session.needs_you]) ===
        JSON.stringify([other.kanban_blocked, other.kanban_blocked_on, other.needs_you]) &&
      session.kanban_unseen === other.kanban_unseen &&
      session.kanban_attention === other.kanban_attention &&
      // Evidence can become unknown, stale, or fresh without a count change.
      // Publish those transitions too; archive guards consume these stamps.
      session.kanban_observed_at === other.kanban_observed_at &&
      session.kanban_unknown === other.kanban_unknown &&
      session.kanban_remote_stale === other.kanban_remote_stale &&
      sameExecutionObservations(session, other) &&
      session.goal_active === other.goal_active &&
      session.cron_active === other.cron_active &&
      session.background_active === other.background_active &&
      backgroundProcessKey(session) === backgroundProcessKey(other) &&
      sameStatusStamps(session, other)
    )
  })
}

// FNV-1a over role/timestamp/content.
function hashString(hash: number, value: string): number {
  let next = hash

  for (let i = 0; i < value.length; i++) {
    next ^= value.charCodeAt(i)
    next = Math.imul(next, 16777619)
  }

  return next >>> 0
}

/** Cheap sidebar-row fingerprint. Used to skip a 120-row transcript fetch
 *  when message_count, last_active, and preview have not moved (#95767). */
export function sessionListFingerprint(session: {
  last_active?: number
  message_count?: number
  preview?: null | string
}): string {
  return `${session.message_count ?? 0}:${session.last_active ?? 0}:${session.preview ?? ''}`
}

/** Transcript fingerprint for the active-messaging-session poll. */
export function sessionMessagesSignature(messages: SessionMessage[]): string {
  let hash = 2166136261

  for (const m of messages) {
    hash = hashString(hash, m.role)
    hash = hashString(hash, String(m.timestamp ?? ''))
    hash = hashString(hash, typeof m.content === 'string' ? m.content : (JSON.stringify(m.content) ?? ''))
  }

  return `${messages.length}:${hash}`
}
