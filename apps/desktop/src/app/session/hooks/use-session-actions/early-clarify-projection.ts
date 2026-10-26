import { type ChatMessage, QUESTION_CARD_TOOLS } from '@/lib/chat-messages'

/** Drop the pre-hydration request-id row without copying the other messages.
 *  A copied clarify row looks like a concurrent edit and hydration keeps both. */
export function withoutEarlyClarifyProjection(messages: ChatMessage[], requestId: string): ChatMessage[] {
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
