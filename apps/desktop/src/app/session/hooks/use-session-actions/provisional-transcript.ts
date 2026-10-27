import type { SessionInfo } from '@/hermes'
import type { ChatMessage } from '@/lib/chat-messages'
import { markOpenPaint } from '@/lib/open-timing'
import { $messages, setMessages } from '@/store/session'
import type { SessionProfileRoute } from '@/store/session-request-router'
import { loadTranscriptTail, type TranscriptTailScope } from '@/store/transcript-tail-cache'

import { chatMessageArraysEquivalent } from './utils'

export function transcriptRestScope(
  owner: SessionProfileRoute | undefined,
  stored: SessionInfo | undefined,
  ambientConnectionId: string
): TranscriptTailScope | undefined {
  const connectionId = owner ? owner.connectionId : stored?.connection_id || ambientConnectionId
  const profile = owner?.targetProfile || owner?.profile || stored?.profile

  return connectionId ? { connectionId, profile: profile || 'default' } : profile || undefined
}

/** Display-only cache paint: never becomes runtime or persisted-display authority. */
export function provisionalTranscriptPaint(storedSessionId: string, isCurrent: () => boolean) {
  let messages: ChatMessage[] | null = null
  let scopeKey: string | undefined

  return {
    get messages() {
      return messages
    },
    paint(scope: TranscriptTailScope | undefined) {
      if (!isCurrent()) {
        return
      }

      const nextKey = JSON.stringify(scope)

      if (messages !== null && nextKey !== scopeKey) {
        if ($messages.get() === messages) {
          setMessages([])
        }

        messages = null
      }

      scopeKey = nextKey

      // An unknown owner is not permission to read a legacy unscoped entry.
      if (!scope || messages !== null || $messages.get().length > 0) {
        return
      }

      messages = loadTranscriptTail(storedSessionId, scope)

      if (messages) {
        setMessages(messages)
        markOpenPaint(storedSessionId, 'cache', messages.length)
      }
    },
    /** [daveey] Swap the display-only paint for a fresher page (still display-only:
     *  the resume's reconcile treats it exactly like the cached paint). Only while
     *  nothing else owns the view; keeps the current array when equivalent, so an
     *  unchanged session never re-renders. Returns whether `next` is on screen. */
    replace(next: ChatMessage[]): { changed: boolean; shown: boolean } {
      const current = $messages.get()

      if (!isCurrent() || next.length === 0 || (current.length > 0 && current !== messages)) {
        return { changed: false, shown: false }
      }

      if (current.length > 0 && chatMessageArraysEquivalent(current, next)) {
        messages = current

        return { changed: false, shown: true }
      }

      messages = next
      setMessages(next)

      return { changed: true, shown: true }
    }
  }
}
