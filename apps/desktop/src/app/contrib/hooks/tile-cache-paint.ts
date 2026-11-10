/**
 * [daveey] Paint a tile from the local transcript cache instead of waiting on REST.
 *
 * A tile (every `@session` ref click, ⌘-click, "open beside") used to show a
 * spinner until both `session.resume` AND the latest-page REST read returned;
 * hub REST is 1-3 s under load while the resume RPC rides an open socket. When
 * the cache holds the session (prefetched because a ref chip rendered, or
 * because it is recent), the tile now publishes the cached rows as soon as
 * the resume binds a runtime, and the REST page reconciles them when it lands
 * (same merge the warm-refresh path uses). With no cache entry, or a REST page
 * that already arrived, nothing changes.
 */
import { type ChatMessage, toChatMessages } from '@/lib/chat-messages'
import { startTileOpenTiming } from '@/lib/open-timing'
import { loadTranscriptTail, saveTranscriptTail, type TranscriptTailScope } from '@/store/transcript-tail-cache'
import type { SessionMessage } from '@/types/hermes'

export interface TrackedPromise<T> {
  readonly promise: Promise<T>
  readonly settled: boolean
}

/** Wrap a promise so callers can ask synchronously whether it has settled. */
export function trackSettled<T>(promise: Promise<T>): TrackedPromise<T> {
  let settled = false

  const done = () => {
    settled = true
  }

  promise.then(done, done)

  return {
    promise,
    get settled() {
      return settled
    }
  }
}

/** Cached transcript a tile may paint while its REST page is still in flight, or null (no scope, REST already
 *  landed, or an explicit refresh whose caller awaits fresh rows). */
export function tileCachedPaint(
  storedSessionId: string,
  scope: TranscriptTailScope | undefined,
  rest: TrackedPromise<unknown>,
  refresh = false
): ChatMessage[] | null {
  if (refresh || rest.settled || !scope) {
    return null
  }

  return loadTranscriptTail(storedSessionId, scope)
}

export type TileOpenTiming = ReturnType<typeof startTileOpenTiming>
export type TilePaint = 'cache' | 'kept' | 'rest'

/** Open timing for a tile resume; an authoritative (live-snapshot) resume is not an open and is not timed. */
export function startTileTiming(storedSessionId: string, authoritative: boolean): TileOpenTiming | null {
  return authoritative ? null : startTileOpenTiming(storedSessionId)
}

/** Stamp the resolved owner onto the timing line. */
export function noteTileOwner(timing: TileOpenTiming | null, owner: unknown): void {
  timing?.stage('owner')

  if (timing && owner && typeof owner === 'object' && 'connectionId' in owner) {
    timing.record.connection = String((owner as { connectionId?: unknown }).connectionId ?? '') || undefined
  }
}

/** [daveey] A tile open that throws still writes its line (`outcome:"failed"`), then rethrows: a failed ref click
 *  must be distinguishable from one that never happened. */
export function failTileOpen(timing: TileOpenTiming | null): (error: unknown) => never {
  return error => {
    timing?.reconciled('failed')

    throw error
  }
}

/** What the tile's first publish showed: kept rows already on screen, the cache, or the REST page. */
export function tilePaintSource(
  authoritative: boolean,
  previous: readonly unknown[],
  cachedPaint: ChatMessage[] | null
): TilePaint {
  if (authoritative || previous.length > 0) {
    return 'kept'
  }

  return cachedPaint ? 'cache' : 'rest'
}

/** Rows for a tile with nothing on screen yet: the cache paint, else the REST page, else the resume's own rows. */
export function tileFirstMessages(
  cachedPaint: ChatMessage[] | null,
  prefetch: { messages?: SessionMessage[] } | null,
  resumed: { messages?: SessionMessage[] } | undefined
): ChatMessage[] {
  return cachedPaint ?? toChatMessages(prefetch?.messages ?? resumed?.messages ?? [])
}

interface SettleTileOpen {
  cachedPaint: ChatMessage[] | null
  painted: TilePaint
  prefetch: { messages?: SessionMessage[] } | null
  prefetchPromise: Promise<{ messages?: SessionMessage[] } | null>
  scope: TranscriptTailScope | undefined
  storedSessionId: string
  timing: TileOpenTiming | null
  /** Publish the REST page over the cached paint; false when the tile was rebound meanwhile. */
  publish: (fresh: ChatMessage[], cachedPaint: ChatMessage[]) => { changed: boolean; ours: boolean }
}

/**
 * Finish a tile open. A cache paint is reconciled when the REST page lands
 * (and the page refreshes the cache); otherwise the open is already final.
 */
export function settleTileOpen(args: SettleTileOpen): void {
  const { cachedPaint, painted, prefetch, timing } = args

  if (painted !== 'cache' || !cachedPaint) {
    if (painted === 'rest') {
      timing?.paint('rest', prefetch?.messages?.length ?? 0)
    }

    // [daveey] Keep the cache as warm as a main-pane open does: the next open of this (often old, unlisted)
    // session paints from the page this one waited on.
    if (prefetch?.messages?.length) {
      saveTranscriptTail(args.storedSessionId, prefetch.messages, args.scope)
    }

    timing?.reconciled(painted === 'kept' ? 'warm' : prefetch ? 'rest' : 'failed')

    return
  }

  timing?.paint('cache', cachedPaint.length)

  void args.prefetchPromise.then(page => {
    if (!page?.messages?.length) {
      timing?.reconciled('failed')

      return
    }

    saveTranscriptTail(args.storedSessionId, page.messages, args.scope)
    const { changed, ours } = args.publish(toChatMessages(page.messages), cachedPaint)
    timing?.reconciled(ours ? 'rest' : 'abandoned', ours ? changed : undefined)
  })
}
