import { type ChatMessage, QUESTION_CARD_TOOLS } from '@/lib/chat-messages'

import { overlayConcurrentMessageChanges } from './utils'

/** Overlay what changed in the cache (`current`, absent for an evicted cache)
 *  while hydration awaited onto `messages`.
 *  The early publish may have appended a synthetic request-id row so the hold
 *  could not hide the question. Drop it before overlaying concurrent edits; the
 *  projection after this re-derives one row. */
export function overlayCacheWithoutEarlyClarify(
  messages: ChatMessage[],
  baseline: ChatMessage[],
  current: ChatMessage[] | undefined,
  requestId: string | undefined
): ChatMessage[] {
  return current
    ? overlayConcurrentMessageChanges(messages, baseline, withoutEarlyClarifyProjection(current, requestId))
    : messages
}

/** Drop the pre-hydration request-id row without copying the other messages.
 *  A copied clarify row looks like a concurrent edit and hydration keeps both.
 *  No pending clarify (`requestId` undefined) leaves `messages` as they are. */
export function withoutEarlyClarifyProjection(messages: ChatMessage[], requestId: string | undefined): ChatMessage[] {
  if (!requestId) {
    return messages
  }

  let changed = false
  const next: ChatMessage[] = []

  for (const message of messages) {
    const parts = message.parts.filter(
      part =>
        !(
          part.type === 'tool-call' &&
          QUESTION_CARD_TOOLS.has(part.toolName) &&
          part.result === undefined &&
          part.toolCallId === requestId
        )
    )

    if (parts.length === message.parts.length) {
      next.push(message)

      continue
    }

    changed = true

    if (parts.length > 0) {
      next.push({ ...message, parts })
    }
  }

  return changed ? next : messages
}
