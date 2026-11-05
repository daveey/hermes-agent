import { getSessionMessages } from '@/api/sessions'
import type { SessionInfo, SessionMessage } from '@/types/hermes'

import { getQueuedPrompts, type QueuedPromptEntry, queuedSubmissionId, writeSessionQueue } from './composer-queue'
import { forgetPendingSubmission } from './pending-submissions'
import { $sessions, resolveComposerSessionKey } from './session'
import { $workingSessionIds } from './session-states'

// [daveey] An `unknown` queue card is a turn the owner lost in a restart: its FIFO pauses and the chat paints red
// until the owner's next pending snapshot drops it, or the user's Discard. Only a provably local route gets that
// snapshot in the background (owner-queue-reconcile), so on a hub route a card whose message DID run again (the
// owner replayed it, or the user re-sent it) kept the chat red after everything had finished, and the backend's own
// view (`sessions outcome`) said nothing was wrong. The stored transcript answers it passively: a REST read, never
// a `session.resume` (which can start a billed turn on a legacy backend).
//
// A card ran when, newer than the card (less clock skew), a user row carries its submission id or its words (a
// merged turn joins queued sends, so the words may be part of the row) AND an assistant reply answers that row's
// turn before the next turn starts (a later user row not stored as `_queued_prompt`). The reply is required: the
// lost run itself persisted its user row before the restart cut it, and that row alone says
// nothing about whether it finished. Anything else (no match, no reply, a failed read) leaves the card as it was.
// The card leaves the journal entirely rather than as `retired`: if the owner still lists it `unknown` in a later
// snapshot, that listing repaints it with its Discard.

const PAGE = 50
const SKEW_S = 10 * 60

const textOf = (value: unknown): string => {
  if (typeof value === 'string') {
    return value
  }

  if (Array.isArray(value)) {
    return value
      .map(part => (part && typeof part === 'object' && typeof part.text === 'string' ? part.text : ''))
      .join('\n')
  }

  return ''
}

const normalized = (text: string) => text.replace(/\s+/g, ' ').trim()

function metadataOf(message: SessionMessage): Record<string, unknown> {
  let metadata: unknown = message.display_metadata

  if (typeof metadata === 'string') {
    try {
      metadata = JSON.parse(metadata)
    } catch {
      return {}
    }
  }

  return metadata && typeof metadata === 'object' ? (metadata as Record<string, unknown>) : {}
}

const isReply = (message: SessionMessage) =>
  message.role === 'assistant' && normalized(textOf(message.content)).length > 0

// A user row stored while a turn ran (`_queued_prompt`) rides that turn; any other user row starts the next one.
const startsTurn = (message: SessionMessage) => metadataOf(message)._queued_prompt !== true

/** Whether a reply answers the turn `ordered[index]` belongs to, before the next turn starts. */
const answeredInTurn = (ordered: readonly SessionMessage[], index: number): boolean => {
  for (const message of ordered.slice(index + 1)) {
    if (message.role === 'user' && startsTurn(message)) {
      return false
    }

    if (isReply(message)) {
      return true
    }
  }

  return false
}

/** Whether `messages` (any order) show `card` sent after it was queued and answered. */
export function cardRanToReply(card: QueuedPromptEntry, messages: readonly SessionMessage[]): boolean {
  const words = normalized(card.text)
  const since = card.queuedAt / 1000 - SKEW_S
  const ordered = [...messages].sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0))

  return ordered.some((message, index) => {
    if (message.role !== 'user' || (message.timestamp ?? 0) < since) {
      return false
    }

    const sent =
      metadataOf(message).submission_id === queuedSubmissionId(card) ||
      (words.length > 0 && normalized(textOf(message.content)).includes(words))

    return sent && answeredInTurn(ordered, index)
  })
}

const unknownCards = (key: string) => getQueuedPrompts(key).filter(entry => entry.serverStatus === 'unknown')

/** Settle the `unknown` cards of `key` the stored transcript shows ran to a reply; true when any left. */
export async function settleLandedUnknownCards(key: string): Promise<boolean> {
  if (!unknownCards(key).length) {
    return false
  }

  let messages: SessionMessage[]

  try {
    ;({ messages } = await getSessionMessages(
      key,
      undefined,
      { includeCompacted: true, limit: PAGE, order: 'latest' },
      { passive: true }
    ))
  } catch {
    return false
  }

  // Re-read after the await: a snapshot or a Discard may have moved the queue meanwhile.
  const landed = new Set(
    unknownCards(key)
      .filter(card => cardRanToReply(card, messages))
      .map(card => card.id)
  )

  if (!landed.size) {
    return false
  }

  landed.forEach(id => forgetPendingSubmission(key, id))
  writeSessionQueue(
    key,
    getQueuedPrompts(key).filter(entry => !(entry.serverStatus === 'unknown' && landed.has(entry.id)))
  )

  return true
}

// The row stamp a check was last made at: re-read only once the row moved (a new message, a turn ending).
const checkedAt = new Map<string, string>()
const inFlight = new Set<string>()

const rowStamp = (key: string, row: SessionInfo) =>
  [row.message_count ?? 0, row.last_active ?? 0, ...unknownCards(key).map(card => card.id)].join(':')

function rowFor(key: string, sessions: readonly SessionInfo[]): SessionInfo | undefined {
  return sessions.find(row => row.id === key || resolveComposerSessionKey(row.id, sessions) === key)
}

/** On each session-list refresh, check every chat painted red by an `unknown` card whose row shows no live turn. */
export function checkLandedUnknownCards(pausedKeys: readonly string[]): void {
  const sessions = $sessions.get()
  const working = new Set($workingSessionIds.get())

  for (const key of pausedKeys) {
    const row = rowFor(key, sessions)

    if (!row || row.turn_live === true || working.has(key) || working.has(row.id) || inFlight.has(key)) {
      continue
    }

    const stamp = rowStamp(key, row)

    if (checkedAt.get(key) === stamp) {
      continue
    }

    checkedAt.set(key, stamp)
    inFlight.add(key)
    void settleLandedUnknownCards(key).finally(() => inFlight.delete(key))
  }
}

export const resetLandedUnknownChecksForTests = (): void => {
  checkedAt.clear()
  inFlight.clear()
}

/** [daveey] Whether the row's red comes from a queue card a restart may have lost (the stripe tooltip names it, so
 *  the user and an agent reading the row both see that the cause is this app's queue, not the backend's status). */
export function stuckOnPausedQueue(
  sessionId: string,
  pausedIds: readonly string[],
  sessions: readonly SessionInfo[]
): boolean {
  const key = resolveComposerSessionKey(sessionId, sessions) ?? sessionId

  return pausedIds.includes(key) || pausedIds.includes(sessionId)
}
