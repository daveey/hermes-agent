/**
 * [daveey] Cached rows of unreachable gateways as fallback inputs to the
 * sidebar's ordinary list (live rows win). A cached row opens read-only from
 * the local transcript cache; without a cached transcript it is refused with
 * the honest "can't open while offline" notice.
 */
import { useStore } from '@nanostores/react'
import { useCallback, useMemo } from 'react'

import type { SessionInfo } from '@/hermes'
import { markOpenClick } from '@/lib/open-timing'
import { clearAsleepOpen, hasCachedTranscriptFor, markAsleepOpen } from '@/store/asleep-session'
import { $sidebarFallbackSessions } from '@/store/boot-snapshot'
import { notify } from '@/store/notifications'
import { offlineOpenNotice } from '@/store/offline-session-actions'
import { $sessionListFreshness, mergeOfflineFallback } from '@/store/offline-sessions'

export function useOfflineFallback(
  liveSessions: SessionInfo[],
  resumeSession: (sessionId: string, session?: SessionInfo) => void
) {
  // [daveey] Plugin rows of unreachable gateways + the cold-start boot snapshot's rows.
  const offlineSessions = useStore($sidebarFallbackSessions)
  const freshness = useStore($sessionListFreshness)

  const sessions = useMemo(
    () => mergeOfflineFallback(liveSessions, offlineSessions, freshness),
    [liveSessions, offlineSessions, freshness]
  )

  const onResumeSession = useCallback(
    (sessionId: string, session?: SessionInfo) => {
      markOpenClick(sessionId, session?.connection_id)

      if (session?.offline_cached) {
        // [daveey] Its gateway is unreachable: open the copy cached on this Mac
        // read-only (resumeSession never dials it). Without one, refuse as before.
        if (!hasCachedTranscriptFor(session)) {
          notify({ durationMs: 6_000, kind: 'warning', message: offlineOpenNotice(session) })

          return
        }

        markAsleepOpen(session)
      } else {
        clearAsleepOpen()
      }

      resumeSession(sessionId, session)
    },
    [resumeSession]
  )

  return { onResumeSession, sessions }
}
