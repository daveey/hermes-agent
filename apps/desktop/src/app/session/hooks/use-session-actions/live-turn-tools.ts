import { textWithoutReferenceLines } from '@/components/assistant-ui/reference-kinds'
import { type ChatMessage, chatMessageText, sameAttachmentTurn } from '@/lib/chat-messages'

import { conflictingTranscriptIdentity } from './pending-turn-identity'

/** Reuse the deduplication boundary, never match an older acknowledged repeat. */
export function optimisticPromptTwin(message: ChatMessage, candidates: ChatMessage[]): ChatMessage | undefined {
  if (message.role !== 'user' || !message.id.startsWith('user-')) {
    return undefined
  }

  return candidates.find(
    candidate =>
      !conflictingTranscriptIdentity(message, candidate) &&
      (textWithoutReferenceLines(chatMessageText(candidate)) === textWithoutReferenceLines(chatMessageText(message)) ||
        sameAttachmentTurn(candidate, message))
  )
}

function completedAnswerCoversTools(next: ChatMessage[], live: ChatMessage[]): boolean {
  const pending = live.findLast(message => message.pending)

  if (!pending) {
    return false
  }

  const lastTool = pending.parts.findLastIndex(part => part.type === 'tool-call')

  if (
    lastTool < 0 ||
    pending.parts.some(part => part.type === 'tool-call' && part.result === undefined && part.completedAt === undefined)
  ) {
    return false
  }

  const answer = pending.parts
    .slice(lastTool + 1)
    .flatMap(part => (part.type === 'text' ? [part.text] : []))
    .join('')
    .trim()

  const tail = next.at(-1)

  return Boolean(
    answer && tail?.role === 'assistant' && !tail.pending && chatMessageText(tail).trim().startsWith(answer)
  )
}

/** A persisted prefix is not an acknowledgement of the active turn's tool state.
 * Match the prompt by identity, never prose; the next prompt closes ownership.
 * Reconcile by call ID so a page-local fold and a live segment paint only once.
 */
export function retainLiveTurnTools(
  next: ChatMessage[],
  previous: ChatMessage[],
  promptTwin?: ChatMessage
): ChatMessage[] {
  const userIndex = previous.findLastIndex(message => message.role === 'user')
  const user = previous[userIndex]
  const live = previous.slice(userIndex + 1)

  if (
    !user ||
    !live.some(message => message.role === 'assistant' && message.pending) ||
    completedAnswerCoversTools(next, live)
  ) {
    return next
  }

  const nextUser = next.findLastIndex(message => message.role === 'user')
  const prompt = next[nextUser]

  const identity = promptTwin ?? user

  if (!prompt || !(prompt.id === identity.id || (identity.rowId !== undefined && prompt.rowId === identity.rowId))) {
    return next
  }

  const tools = new Map(
    live.flatMap(message =>
      message.parts.flatMap(part =>
        part.type === 'tool-call' && part.toolCallId ? [[part.toolCallId, part] as const] : []
      )
    )
  )

  if (!tools.size) {
    return next
  }

  const seen = new Set<string>()
  let changed = false

  const merged = next.map((message, index) => {
    if (index <= nextUser) {
      return message
    }

    const parts = message.parts.flatMap(part => {
      if (part.type !== 'tool-call' || !part.toolCallId || !tools.has(part.toolCallId)) {
        return [part]
      }

      if (seen.has(part.toolCallId)) {
        changed = true

        return []
      }

      seen.add(part.toolCallId)
      const local = tools.get(part.toolCallId)!
      // A terminal server result wins; an older tool-start cannot erase a result.
      const replacement = part.result !== undefined || part.completedAt !== undefined ? part : local
      changed ||= replacement !== part

      return [replacement]
    })

    return parts.length === message.parts.length && parts.every((part, index) => part === message.parts[index])
      ? message
      : { ...message, parts }
  })

  const missing = [...tools.entries()].filter(([id]) => !seen.has(id)).map(([, part]) => part)

  if (missing.length) {
    const tail = merged.findLastIndex((message, index) => index > nextUser && message.role === 'assistant')

    if (tail >= 0) {
      merged[tail] = { ...merged[tail], parts: [...merged[tail].parts, ...missing] }
    } else {
      const source = live.find(message => message.parts.some(part => part.type === 'tool-call'))!
      merged.push({ ...source, parts: missing })
    }
  }

  return changed || missing.length ? merged : next
}
