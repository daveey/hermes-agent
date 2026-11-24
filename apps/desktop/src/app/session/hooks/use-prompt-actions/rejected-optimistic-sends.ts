import { notifyError } from '@/store/notifications'

/**
 * Optimistic user bubbles whose send was rejected before it left the client
 * (the chat had no live runtime and `session.resume` failed or timed out).
 *
 * submit.ts drops such a bubble from the view, but a resume that started while
 * the bubble was still shown holds it in its start-of-resume snapshot and would
 * paint it back as a local pending turn: the message then looks delivered
 * although it only went back to the composer draft. resumeSession filters that
 * snapshot through `withoutRejectedOptimisticSends` at every repaint.
 */
const MAX_REJECTED = 64
const rejectedOptimisticIds = new Set<string>()

/** The bubble id of a send that starts now. A retry of a restored draft reuses
 *  its submission id, so an earlier rejection under it no longer applies. */
export function startOptimisticSend(submissionId: string): string {
  const optimisticId = `user-${submissionId}`

  rejectedOptimisticIds.delete(optimisticId)

  return optimisticId
}

function markOptimisticSendRejected(optimisticId: string): void {
  rejectedOptimisticIds.delete(optimisticId)
  rejectedOptimisticIds.add(optimisticId)

  for (const oldest of rejectedOptimisticIds) {
    if (rejectedOptimisticIds.size <= MAX_REJECTED) {
      break
    }

    rejectedOptimisticIds.delete(oldest)
  }
}

/**
 * The chat has no live runtime and rebinding one failed (a starved hub times
 * `session.resume` out after 30s), so this send never left. The composer
 * restores the words into the draft when submit returns `false`; without a
 * notice the user sees nothing and takes the message for delivered.
 *
 * Call before the abort drops the bubble. A deliberate session switch (`drift`)
 * stays silent, and so does a queued entry (`quiet`): it stays in its queue, it
 * is not "back in the composer". A `hidden` send is machine text that is
 * dropped, never restored.
 */
export function rejectSendOnFailedResume(
  err: unknown,
  send: {
    copy: { promptFailed: string; promptNotSent: string }
    drift: null | string
    hidden: boolean
    optimisticId: string
    quiet: boolean
  }
): void {
  markOptimisticSendRejected(send.optimisticId)

  if (send.drift) {
    console.warn('[submit-drift-abort]', send.drift, { phase: 'resume-failed' })
  } else if (!send.quiet) {
    notifyError(err, send.hidden ? send.copy.promptFailed : send.copy.promptNotSent)
  }
}

export function withoutRejectedOptimisticSends<T extends { id: string }>(messages: T[]): T[] {
  if (rejectedOptimisticIds.size === 0 || !messages.some(message => rejectedOptimisticIds.has(message.id))) {
    return messages
  }

  return messages.filter(message => !rejectedOptimisticIds.has(message.id))
}

export function resetRejectedOptimisticSendsForTests(): void {
  rejectedOptimisticIds.clear()
}
