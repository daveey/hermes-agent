import { chatMessageText } from './parts'
import type { ChatMessage } from './types'

export const NO_REPLY_ERROR = 'Hermes ended this turn without a reply.'

/** The notice the silent-turn check stamps on a turn it read as over with no
 *  reply (store/session-no-reply.ts). The text also names it: a bubble can
 *  lose its descriptor on the way through a cache and keep only the string. */
export function isNoReplyNotice(message: ChatMessage): boolean {
  return (
    message.role === 'assistant' && (message.errorSurface?.code === 'no_reply' || message.error === NO_REPLY_ERROR)
  )
}

const visible = (messages: ChatMessage[]) => messages.filter(message => !message.hidden)
const userText = (message: ChatMessage) => chatMessageText(message).replace(/\s+/g, ' ').trim()

function tailUser(messages: ChatMessage[]): ChatMessage | undefined {
  return visible(messages).findLast(message => message.role === 'user')
}

function tailTurnHasReply(messages: ChatMessage[]): boolean {
  for (const message of visible(messages).reverse()) {
    if (message.role === 'user') {
      return false
    }

    if (message.role === 'assistant' && !isNoReplyNotice(message) && chatMessageText(message).trim()) {
      return true
    }
  }

  return false
}

/** Same prompt row: by id, by stored row, or (an optimistic local row has
 *  neither in common with its stored twin) by text at the same repeat count. */
function sameTailPrompt(next: ChatMessage[], current: ChatMessage[]): boolean {
  const stored = tailUser(next)
  const local = tailUser(current)

  if (!stored || !local) {
    return false
  }

  if (stored.id === local.id || (stored.rowId !== undefined && stored.rowId === local.rowId)) {
    return true
  }

  const repeats = (messages: ChatMessage[], text: string) =>
    visible(messages).filter(message => message.role === 'user' && userText(message) === text).length

  const text = userText(stored)

  return text === userText(local) && repeats(next, text) === repeats(current, text)
}

/**
 * The silent-turn check can read a running turn as ended (its liveness
 * snapshot missed the runtime) and stamp the no-reply notice on it. Once the
 * refreshed transcript holds that turn's reply the notice is disproved: clear
 * it, so the refresh neither keeps the stamped bubble as a second one under
 * the reply nor folds the error onto the reply. A turn with no stored reply
 * keeps its notice.
 */
export function withoutDisprovedNoReplyNotice(next: ChatMessage[], current: ChatMessage[]): ChatMessage[] {
  if (!current.some(isNoReplyNotice) || !tailTurnHasReply(next) || !sameTailPrompt(next, current)) {
    return current
  }

  const turnStart = current.lastIndexOf(tailUser(current) as ChatMessage)

  return current.map((message, index) => {
    if (index < turnStart || !isNoReplyNotice(message)) {
      return message
    }

    const { error: _error, errorSurface: _surface, ...rest } = message

    return rest
  })
}
