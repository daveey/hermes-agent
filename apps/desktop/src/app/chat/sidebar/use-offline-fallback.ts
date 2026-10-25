/**
 * [daveey] Cached rows of unreachable gateways as fallback inputs to the
 * sidebar's ordinary list (live rows win), plus the honest "can't open while
 * offline" guard for resuming a cached row.
 */
import { useStore } from '@nanostores/react'
import { useCallback, useMemo } from 'react'

import type { SessionInfo } from '@/hermes'
import { notify } from '@/store/notifications'
import { offlineOpenNotice } from '@/store/offline-session-actions'
import { $offlineSessions, mergeOfflineFallback } from '@/store/offline-sessions'

export function useOfflineFallback(
  liveSessions: SessionInfo[],
  resumeSession: (sessionId: string, session?: SessionInfo) => void
) {
  const offlineSessions = useStore($offlineSessions)
  const sessions = useMemo(() => mergeOfflineFallback(liveSessions, offlineSessions), [liveSessions, offlineSessions])

  const onResumeSession = useCallback(
    (sessionId: string, session?: SessionInfo) => {
      if (session?.offline_cached) {
        notify({ durationMs: 6_000, kind: 'warning', message: offlineOpenNotice(session) })

        return
      }

      resumeSession(sessionId, session)
    },
    [resumeSession]
  )

  return { onResumeSession, sessions }
}
