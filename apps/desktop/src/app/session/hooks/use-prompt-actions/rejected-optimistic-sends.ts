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

interface UnsentCopy {
  promptBackInComposer: string
  promptFailed: string
  promptNotQueued: string
  promptNotSent: string
  promptSendUnconfirmed: string
  promptStillInComposer: string
}

interface UnsentSend {
  copy: UnsentCopy
  /** `sessionDriftReason()`: the user moved to another chat. */
  drift: null | string
  optimisticId: string
  options: undefined | { displayKind?: 'hidden'; fromQueue?: boolean; unsent?: 'kept' | 'restored' }
  /** This identity was sent before and its outcome never came back. */
  uncertain: boolean
}

/** What happened to the words, then where they are now (see `SubmitTextOptions.unsent`). */
function unsentNotice({ copy, options, uncertain }: UnsentSend): string {
  if (options?.displayKind === 'hidden') {
    // Machine text: dropped, never restored anywhere.
    return copy.promptFailed
  }

  const kept = options?.unsent === 'kept'
  const what = uncertain ? copy.promptSendUnconfirmed : kept ? copy.promptNotQueued : copy.promptNotSent

  // Only a caller that says so gets a place named: a voice turn, the quick-entry
  // window or the pet restore nothing, and an external request is re-queued.
  if (kept) {
    return `${what} ${copy.promptStillInComposer}`
  }

  return options?.unsent === 'restored' ? `${what} ${copy.promptBackInComposer}` : what
}

/**
 * The chat has no live runtime and rebinding one failed (a starved hub times
 * `session.resume` out after 30s), so this attempt never left. The composer
 * restores the words into the draft when submit returns `false` (other callers
 * restore nothing); without a notice the user sees nothing and takes the
 * message for delivered.
 *
 * Call before the abort drops the bubble. A deliberate session switch (`drift`)
 * stays silent, and so does a drain of an entry that stays in its queue
 * (`fromQueue` without `unsent: 'kept'`): the queue shows it and retries it.
 */
export function rejectSendOnFailedResume(err: unknown, send: UnsentSend): void {
  markOptimisticSendRejected(send.optimisticId)

  if (send.drift) {
    console.warn('[submit-drift-abort]', send.drift, { phase: 'resume-failed' })
  } else if (!send.options?.fromQueue || send.options.unsent === 'kept') {
    notifyError(err, unsentNotice(send))
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
