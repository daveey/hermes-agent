import type { ChatMessage } from '@/lib/chat-messages'

import { withoutRejectedOptimisticSends } from '../use-prompt-actions/rejected-optimistic-sends'

import { preserveLocalPendingTurnMessages } from './utils'

/**
 * A resume carries the local pending turn it saw when it started across each
 * of its repaints. Filtered at the repaint, not when the snapshot is taken: a
 * send that was rejected while the resume was in flight (its own
 * `session.resume` failed) must not come back as a bubble.
 */
export function preservePendingTurnAcrossResume(
  nextMessages: ChatMessage[],
  resumeStartMessages: ChatMessage[]
): ChatMessage[] {
  return preserveLocalPendingTurnMessages(nextMessages, withoutRejectedOptimisticSends(resumeStartMessages))
}
