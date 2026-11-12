import type { MutableRefObject } from 'react'

import { closeSessionTile, dropSessionState } from '@/store/session-states'

import type { ClientSessionState } from '../../../types'

/**
 * [daveey] A tiled copy of a deleted or archived session must not outlive it: collapse the
 * pane and evict its mirrored runtime state so nothing submits to (or renders) that session.
 * Returns the tile's runtime id, if it had one.
 */
export function closeStoredSessionTile(
  storedSessionId: string,
  runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>>,
  sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>>
): string | undefined {
  const tiledRuntimeId = runtimeIdByStoredSessionIdRef.current.get(storedSessionId)
  closeSessionTile(storedSessionId)

  if (tiledRuntimeId) {
    runtimeIdByStoredSessionIdRef.current.delete(storedSessionId)
    sessionStateByRuntimeIdRef.current.delete(tiledRuntimeId)
    dropSessionState(tiledRuntimeId)
  }

  return tiledRuntimeId
}
