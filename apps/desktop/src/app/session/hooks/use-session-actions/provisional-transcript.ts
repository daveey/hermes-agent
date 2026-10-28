import type { SessionInfo } from '@/hermes'
import type { ChatMessage } from '@/lib/chat-messages'
import { markOpenPaint } from '@/lib/open-timing'
import { $messages, $selectedStoredSessionId, setMessages } from '@/store/session'
import { sessionOwnerRouteFromRow, type SessionProfileRoute } from '@/store/session-request-router'
import { $sessionTiles } from '@/store/session-states'
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

/**
 * [daveey] Paint a clicked row's cached transcript in the click itself. Opening a session in main routes first and
 * resumes in the route effect, after React has rendered the route change: 80-100 ms of every open (warm reopens,
 * which read no cache, measured the same). Painting here lands the cached rows in the same commit as the route.
 * The resume then repaints the same rows from the same scope, display-only as before.
 *
 * Only when the click loads the session into main: a session already selected or open as a tile is focused, not
 * reloaded, and an asleep gateway's row has its own read-only open. Returns whether it painted.
 */
export function prepaintClickedTranscript(storedSessionId: string, row: SessionInfo | undefined): boolean {
  if (
    !storedSessionId ||
    !row ||
    row.offline_cached ||
    $selectedStoredSessionId.get() === storedSessionId ||
    $sessionTiles.get().some(tile => tile.storedSessionId === storedSessionId)
  ) {
    return false
  }

  const scope = transcriptRestScope(sessionOwnerRouteFromRow(row), row, '')
  const messages = scope ? loadTranscriptTail(storedSessionId, scope) : null

  if (!messages?.length) {
    return false
  }

  setMessages(messages)
  markOpenPaint(storedSessionId, 'cache', messages.length)

  return true
}
