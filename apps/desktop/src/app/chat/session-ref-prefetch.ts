/**
 * [daveey] Warm the transcript cache for a session an `@session:` ref points at.
 *
 * The background prefetcher (electron/transcript-prefetch.ts) keeps only each
 * gateway's PER_GATEWAY most recently active sessions cached. A mention usually
 * names an older session, so its first click waited on a hub REST round trip
 * (1-3 s under load) before anything painted. When a ref chip renders (and again
 * on pointer-enter) we resolve its owner exactly as the click does
 * (`resolveSessionRefOwner`) and ask main to fetch that key into the cache;
 * the click then paints from it (`paint:"cache"` in open-timing).
 *
 * The key is the one the open's cache paint reads (`transcriptRestScope`):
 * the owner's connection and its target profile. An unresolved owner sends
 * nothing (the open would not read an unscoped entry either), and is not
 * remembered, so a later render/hover retries once the session list knows it.
 * Main dedupes, honours MIN_REFETCH_MS and per-gateway backoff; this side only
 * stops one key from re-sending inside the same window.
 */
import { resolveSessionRefOwner } from '@/app/chat/session-ref-owner'
import { parseSessionRefValue } from '@/lib/session-refs'

/** Mirrors electron/transcript-prefetch.ts MIN_REFETCH_MS. */
export const REF_PREFETCH_MIN_MS = 30_000
const SENT_MAX = 1_000

const sent = new Map<string, number>()

export interface SessionRefPrefetchKey {
  connectionId: string
  profile: string
  sessionId: string
}

/** The cache key a click on this ref would paint from, or null when its owner is unknown. */
export function sessionRefPrefetchKey(value: string, originStoredId?: null | string): null | SessionRefPrefetchKey {
  const { sessionId } = parseSessionRefValue(value)

  if (!sessionId) {
    return null
  }

  const owner = resolveSessionRefOwner(value, originStoredId)

  if (!owner) {
    return null
  }

  return {
    connectionId: String(owner.connectionId ?? '').trim(),
    profile: owner.targetProfile || owner.profile || 'default',
    sessionId
  }
}

/** Ask main to warm this ref's session. Returns the key it sent, or null when it sent nothing. */
export function prefetchSessionRef(
  value: string,
  originStoredId?: null | string,
  now: number = Date.now()
): null | SessionRefPrefetchKey {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop?.transcriptCache : undefined

  if (!bridge?.prefetch) {
    return null
  }

  const key = sessionRefPrefetchKey(value, originStoredId)

  if (!key) {
    return null
  }

  const id = JSON.stringify([key.connectionId, key.profile, key.sessionId])
  const last = sent.get(id)

  if (last !== undefined && now - last < REF_PREFETCH_MIN_MS) {
    return null
  }

  sent.delete(id)
  sent.set(id, now)

  for (const stale of sent.keys()) {
    if (sent.size <= SENT_MAX) {
      break
    }

    sent.delete(stale)
  }

  try {
    bridge.prefetch(key)
  } catch {
    return null
  }

  return key
}

/** Test hook: forget what was sent. */
export function resetSessionRefPrefetch(): void {
  sent.clear()
}
