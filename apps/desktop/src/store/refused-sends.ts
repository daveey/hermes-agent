import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'

import { draftKey, stashSessionDraft, takeSessionDraft } from './composer'

// A send the owner admitted whose turn then could not start because another writer held the
// chat's turn lease: a serve backend refuses a held lease at once ("This chat has another active
// writer ... Your message was not processed"), a gateway-hosted turn gives up after its lease wait
// ("kept this session busy too long ... send it again"), and a chat taken over by another window
// refuses the queued turn. The backend stamps each `failure_reason: session_busy`
// (agent/turn_facade_lease.py); the text never reached the model. Enter had already cleared the
// composer, so the words would only live in the transcript bubble: the composer gets them back.
//
// Offered, never re-sent: the user decides. The draft carries no retry identity (the refused
// submission id is admitted and settled, so reusing it would be deduplicated and run nothing), and
// a re-run of that turn from its card (Take over here / Retry) withdraws a still-untouched offer,
// so the send never runs twice.

/** Mark the turn's error bubble (the newest message carrying an error) as a refused send. */
export function markRefusedSendTail(messages: ChatMessage[]): ChatMessage[] {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]

    if (message.role === 'user') {
      return messages
    }

    if (message.error) {
      return message.refusedSend
        ? messages
        : messages.map((item, i) => (i === index ? { ...item, refusedSend: true } : item))
    }
  }

  return messages
}

export interface RefusedSend {
  text: string
}

export interface TailUserTurn {
  /** The newest turn's outcome is a busy-chat refusal. */
  refused: boolean
  text: string
}

/** The transcript's newest user turn and whether a busy-chat refusal answered it. */
export function tailUserTurn(messages: readonly ChatMessage[]): null | TailUserTurn {
  let refused = false

  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]

    if (message.role !== 'user') {
      refused ||= message.refusedSend === true

      continue
    }

    return { refused, text: chatMessageText(message).trim() }
  }

  return null
}

/** The refused send at the transcript's tail: its error is the latest turn's outcome, nothing was
 *  sent after it. Null once a later user turn exists (the user moved on, or already re-sent it). */
export function refusedSendAtTail(messages: readonly ChatMessage[]): null | RefusedSend {
  const tail = tailUserTurn(messages)

  return tail?.refused && tail.text ? { text: tail.text } : null
}

/** The composer's text with a refused send given back: kept as is when it already holds those words. */
export function mergeRefusedText(current: string, text: string): string {
  const words = text.trim()

  if (!current.trim()) {
    return words
  }

  return current.includes(words) ? current : `${current.trimEnd()}\n\n${words}`
}

// Offers survive a reload: a transcript re-read (live bubble, then hydrated row) must not paint
// the same refusal into a composer the user deliberately emptied. One claim per scope, keyed by
// the refused words; a newer user turn at the tail releases it, so the same words refused again
// later are offered again.
const OFFERED_STORAGE_KEY = 'hermes:refused-send-offered:v1'
const MAX_OFFERED = 200

function readOffered(): string[] {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(OFFERED_STORAGE_KEY) || '[]')

    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

function writeOffered(offered: string[]): void {
  try {
    window.localStorage.setItem(OFFERED_STORAGE_KEY, JSON.stringify(offered.slice(-MAX_OFFERED)))
  } catch {
    // Best-effort like the draft itself: quota/private mode never blocks the offer.
  }
}

const scopePrefix = (scope: string | null | undefined) => `${JSON.stringify(draftKey(scope))}\u0000`
const offeredId = (scope: string | null | undefined, text: string) => `${scopePrefix(scope)}${text.trim()}`

/** Claim the one offer of `send` in `scope`: false when it was already given back. */
export function claimRefusedSendOffer(scope: string | null | undefined, send: RefusedSend): boolean {
  const id = offeredId(scope, send.text)
  const offered = readOffered()

  if (offered.includes(id)) {
    return false
  }

  writeOffered([...offered.filter(item => !item.startsWith(scopePrefix(scope))), id])
  lastOffers.set(draftKey(scope), send.text)

  return true
}

/** A newer user turn than the refusal sits at the tail of `scope`: its claim is history. */
export function releaseRefusedSendOffers(scope: string | null | undefined): void {
  const prefix = scopePrefix(scope)
  const offered = readOffered()

  if (offered.some(item => item.startsWith(prefix))) {
    writeOffered(offered.filter(item => !item.startsWith(prefix)))
  }
}

// The words last given back per draft scope, so a re-run of that turn can take them back.
const lastOffers = new Map<string, string>()

const WITHDRAW_EVENT = 'hermes:refused-send-withdraw'

interface WithdrawDetail {
  /** Set by a composer showing `scope`: it judged the live editor itself. */
  handled: boolean
  scope: string
  text: string
}

/**
 * The refused turn is being re-run from its card (reload, Take over here): a composer of `scope`
 * still holding exactly the offered words drops them, or Enter would run the message a second time.
 * Edited words are the user's new message and stay. With no composer showing `scope`, the saved
 * draft is checked the same way.
 */
export function withdrawRefusedSend(scope: string | null | undefined, text: string): void {
  const key = draftKey(scope)
  const offered = lastOffers.get(key)

  if (offered === undefined || offered.trim() !== text.trim() || typeof window === 'undefined') {
    return
  }

  lastOffers.delete(key)
  const detail: WithdrawDetail = { handled: false, scope: key, text: offered }
  window.dispatchEvent(new CustomEvent<WithdrawDetail>(WITHDRAW_EVENT, { detail }))

  if (!detail.handled) {
    const saved = takeSessionDraft(key)

    if (saved.text.trim() === offered.trim()) {
      stashSessionDraft(key, '', saved.attachments)
    }
  }
}

export function onRefusedSendWithdraw(handler: (detail: WithdrawDetail) => void): () => void {
  if (typeof window === 'undefined') {
    return () => undefined
  }

  const listener = (event: Event) => handler((event as CustomEvent<WithdrawDetail>).detail)
  window.addEventListener(WITHDRAW_EVENT, listener)

  return () => window.removeEventListener(WITHDRAW_EVENT, listener)
}

/** Test seam. */
export function resetRefusedSendsForTests(): void {
  lastOffers.clear()

  try {
    window.localStorage.removeItem(OFFERED_STORAGE_KEY)
  } catch {
    // ignore
  }
}
