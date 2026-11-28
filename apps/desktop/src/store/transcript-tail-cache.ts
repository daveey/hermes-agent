import { LATEST_SESSION_MESSAGES_LIMIT } from '@/api/sessions'
import type { SessionMessage } from '@/hermes'
import type { ChatMessage } from '@/lib/chat-messages'
import { toChatMessages, withUniqueToolCallIdsWithinMessage } from '@/lib/chat-messages'

// ── Durable transcript cache ([daveey] instant session loads, C1) ───────────
// The in-memory warm cache (sessionStateByRuntimeIdRef) makes same-window
// re-opens instant, but dies with the window, and a remote gateway's first
// REST read waits seconds on SSH. This cache is a SQLite file owned by
// Electron main (electron/transcript-cache.ts): the raw `/messages` rows of
// recently active sessions on every gateway, refreshed in the background by
// the prefetcher (electron/transcript-prefetch.ts) and by every open's REST
// read. A click paints the cached rows synchronously; the REST read and the
// runtime resume reconcile the authoritative transcript when they land (the
// reconcilers treat the painted cache as "nothing on screen yet").
//
// Rows, not ChatMessages: the cache holds backend truth (what REST returns),
// so a paint converts exactly the rows the next REST page would and an
// unchanged session reconciles to an equivalent transcript (no re-render).
//
// Scoping (#94828): stored ids are only unique within one profile's state.db,
// so entries are keyed by (connectionId, profile, storedId). '' connection
// means the registry primary (main normalizes it). An unknown owner never
// reads: there is no unscoped entry to fall back on.
//
// Same trust domain as state.db on this disk; nothing here leaves the Mac.

/** Owning scope of an entry — same shape the REST layer threads through
 *  `getLatestSessionMessages(id, scope)`. */
export type TranscriptTailScope =
  | null
  | string
  | {
      connectionId?: null | string
      profile?: null | string
    }

interface BridgeKey {
  connectionId: string
  profile: string
  sessionId: string
}

interface BridgeEntry {
  activity: null | number
  messageCount: null | number
  messages: unknown[]
  savedAt: number
}

type Bridge = NonNullable<NonNullable<Window['hermesDesktop']>['transcriptCache']>

// No Electron bridge (unit tests, a browser build): an in-memory twin with the
// same keying, so callers and tests exercise one contract.
const memory = new Map<string, BridgeEntry>()
const memoryKey = (key: BridgeKey) => JSON.stringify([key.connectionId || '', key.profile, key.sessionId])

const memoryBridge: Bridge = {
  clear: () => memory.clear(),
  drop: key => void memory.delete(memoryKey(key)),
  dropEverywhere: sessionId => {
    for (const [entry] of memory) {
      if (JSON.parse(entry)[2] === sessionId) {
        memory.delete(entry)
      }
    }
  },
  migrateProfile: (connectionId, from, to) => {
    for (const [entry, value] of [...memory]) {
      const [conn, profile, id] = JSON.parse(entry)

      if (profile === from && (conn || 'local') === (connectionId || 'local')) {
        memory.delete(entry)
        memory.set(JSON.stringify([conn, to, id]), value)
      }
    }
  },
  put: (key, messages, kind) => {
    const current = memory.get(memoryKey(key))

    if (kind === 'older') {
      if (current) {
        memory.set(memoryKey(key), { ...current, messages: [...messages, ...current.messages] })
      }

      return
    }

    memory.set(memoryKey(key), {
      activity: current?.activity ?? null,
      messageCount: current?.messageCount ?? null,
      messages: messages.slice(-200),
      savedAt: Date.now()
    })
  },
  read: key => memory.get(memoryKey(key)) ?? null,
  stats: async () => ({ bytes: 0, sessions: memory.size })
}

function bridge(): Bridge {
  try {
    return (typeof window !== 'undefined' && window.hermesDesktop?.transcriptCache) || memoryBridge
  } catch {
    return memoryBridge
  }
}

function bridgeKey(storedSessionId: string, scope?: TranscriptTailScope): BridgeKey | null {
  const sessionId = (storedSessionId ?? '').trim()

  if (!sessionId || !scope) {
    return null
  }

  if (typeof scope === 'string') {
    return { connectionId: '', profile: scope.trim() || 'default', sessionId }
  }

  return {
    connectionId: String(scope.connectionId ?? '').trim(),
    profile: String(scope.profile ?? '').trim() || 'default',
    sessionId
  }
}

/** Persist the latest REST page of a session (raw rows, chronological). */
export function saveTranscriptTail(
  storedSessionId: string,
  messages: SessionMessage[],
  scope?: TranscriptTailScope
): void {
  const key = bridgeKey(storedSessionId, scope)

  if (!key || !Array.isArray(messages) || messages.length === 0) {
    return
  }

  try {
    bridge().put(key, messages, 'latest')
  } catch {
    // Non-serializable rows or a torn-down bridge: the paint just won't be instant.
  }
}

/** Persist an older page the user loaded ("Show earlier") above the cached run. */
export function saveOlderTranscriptPage(
  storedSessionId: string,
  messages: SessionMessage[],
  scope?: TranscriptTailScope
): void {
  const key = bridgeKey(storedSessionId, scope)

  if (!key || !Array.isArray(messages) || messages.length === 0) {
    return
  }

  try {
    bridge().put(key, messages, 'older')
  } catch {
    // best effort
  }
}

/** Raw cached entry, or null. */
export function readCachedTranscript(storedSessionId: string, scope?: TranscriptTailScope): BridgeEntry | null {
  const key = bridgeKey(storedSessionId, scope)

  if (!key) {
    return null
  }

  try {
    const entry = bridge().read(key)

    return entry && Array.isArray(entry.messages) && entry.messages.length > 0 ? entry : null
  } catch {
    return null
  }
}

/** Cached transcript for a stored session, converted for display, or null.
 *  By default only the newest LATEST_SESSION_MESSAGES_LIMIT rows: the page the
 *  open's REST read fetches, so an unchanged session reconciles without a
 *  re-render. `all` paints every cached row (read-only open of an asleep
 *  gateway, where nothing will replace it). */
export function loadTranscriptTail(
  storedSessionId: string,
  scope?: TranscriptTailScope,
  options: { all?: boolean } = {}
): ChatMessage[] | null {
  const entry = readCachedTranscript(storedSessionId, scope)

  if (!entry) {
    return null
  }

  try {
    const rows = (
      options.all ? entry.messages : entry.messages.slice(-LATEST_SESSION_MESSAGES_LIMIT)
    ) as SessionMessage[]
    const messages = toChatMessages(rows)

    // Repair, don't just trust: a poisoned entry would be re-read identically
    // on every launch (#87857), so duplicate tool-call ids are renamed at read.
    return messages.length > 0 ? messages.map(withUniqueToolCallIdsWithinMessage) : null
  } catch {
    dropTranscriptTail(storedSessionId, scope)

    return null
  }
}

/** Drop one scope's entry (failed resume / poisoned cache). Other backends'
 *  entries for the same stored id stay intact. */
export function dropTranscriptTail(storedSessionId: string, scope?: TranscriptTailScope): void {
  const key = bridgeKey(storedSessionId, scope)

  if (key) {
    try {
      bridge().drop(key)
    } catch {
      // best effort
    }
  }
}

/** Drop EVERY scope's entry for a deleted stored id (delete path only: a
 *  same-id twin in another profile keeps its entry on a failed resume). */
export function dropTranscriptTailEverywhere(storedSessionId: string): void {
  const id = (storedSessionId ?? '').trim()

  if (id) {
    try {
      bridge().dropEverywhere(id)
    } catch {
      // best effort
    }
  }
}

/** Re-key the local connection's entries after a profile rename. */
export function migrateTranscriptTailsForProfile(oldProfile: string, newProfile: string): void {
  const from = oldProfile.trim() || 'default'
  const to = newProfile.trim() || 'default'

  if (from !== to) {
    try {
      bridge().migrateProfile('local', from, to)
    } catch {
      // best effort
    }
  }
}

/** Wipe the whole cache (connection/mode re-home). */
export function clearTranscriptTails(): void {
  try {
    bridge().clear()
  } catch {
    // best effort
  }
}
