/**
 * [daveey] Forward every gateway socket's `sessions.moved` push to Electron main.
 *
 * The gateway's change watcher names the sessions that got new message rows, at most once
 * a second (`sessions.moved`). Main's background prefetcher (electron/transcript-prefetch.ts)
 * owns the durable transcript cache, so it fetches just the rows after each cached tail the
 * moment they land, instead of the next hub listing pass noticing (up to 30 s + the hub's own
 * hold). Every socket this window holds (the primary and each registry secondary) reaches the
 * plugin event tap stamped with its `connectionId`; an unstamped event (a legacy primary)
 * names the registry primary (''). Main coalesces per session, so the same frame arriving
 * over two sockets or from two windows costs one fetch.
 */
import type { GatewayEvent } from '@hermes/shared'

import { onGatewayEvent } from '@/contrib/events'

type Forward = (connectionId: string, sessions: unknown[]) => void

/** The connection a frame came over and its session list, or null for a frame not worth forwarding. */
export function sessionsMovedForward(event: GatewayEvent): null | { connectionId: string; sessions: unknown[] } {
  const payload = event.payload as { sessions?: unknown } | undefined
  const sessions = Array.isArray(payload?.sessions) ? payload.sessions : []

  if (sessions.length === 0) {
    return null
  }

  const connectionId = (event as { connectionId?: unknown }).connectionId

  return { connectionId: typeof connectionId === 'string' ? connectionId.trim() : '', sessions }
}

/** Subscribe the forwarder; returns its disposer. No bridge (browser host, tests without one): nothing to do. */
export function installTranscriptPushForwarder(
  forward: Forward | undefined = window.hermesDesktop?.transcriptCache?.sessionsMoved
): () => void {
  if (typeof forward !== 'function') {
    return () => {}
  }

  return onGatewayEvent('sessions.moved', event => {
    const message = sessionsMovedForward(event)

    if (message) {
      forward(message.connectionId, message.sessions)
    }
  })
}

if (typeof window !== 'undefined') {
  installTranscriptPushForwarder()
}
