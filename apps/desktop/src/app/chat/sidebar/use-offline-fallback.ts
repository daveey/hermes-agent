/**
 * [daveey] Cached rows of unreachable gateways as fallback inputs to the
 * sidebar's ordinary list (live rows win). A cached row opens read-only from
 * the local transcript cache; without a cached transcript it is refused with
 * the honest "can't open while offline" notice. A stale-list row (its owner failed one
 * list, so it may still be running) resumes live instead, under resume's own
 * timeout, after a notice that names the connection.
 */
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef } from 'react'

import { prepaintClickedTranscript } from '@/app/session/hooks/use-session-actions/provisional-transcript'
import type { SessionInfo } from '@/hermes'
import { markOpenClick } from '@/lib/open-timing'
import { cancelUiLatency, startUiLatency } from '@/lib/ui-latency'
import { clearAsleepOpen, hasCachedTranscriptFor, markAsleepOpen } from '@/store/asleep-session'
import { $sidebarFallbackSessions } from '@/store/boot-snapshot'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { sessionGatewayId } from '@/store/gateway-visibility'
import { notify } from '@/store/notifications'
import { offlineOpenNotice } from '@/store/offline-session-actions'
import { $sessionListFreshness, mergeOfflineFallback, projectTreeSessionRows } from '@/store/offline-sessions'
import { $ownerRowStamps, refreshOwnerRowStamps, rowsNeedingOwnerStamps } from '@/store/owner-row-stamps'
import { $projectTree } from '@/store/project-tree'

const OWNER_STAMP_TICK_MS = 45_000

export function useOfflineFallback(
  liveSessions: SessionInfo[],
  resumeSession: (sessionId: string, session?: SessionInfo) => void
) {
  // [daveey] Plugin rows of unreachable gateways + the cold-start boot snapshot's rows.
  const offlineSessions = useStore($sidebarFallbackSessions)
  const freshness = useStore($sessionListFreshness)
  const projectTree = useStore($projectTree)
  const treeRows = useMemo(() => projectTreeSessionRows(projectTree), [projectTree])

  const ownerStamps = useStore($ownerRowStamps)

  const sessions = useMemo(
    () => mergeOfflineFallback(liveSessions, offlineSessions, freshness, treeRows, ownerStamps),
    [liveSessions, offlineSessions, freshness, treeRows, ownerStamps]
  )

  // [daveey] Awake replica rows the projects tree doesn't carry have no activity stamps: ask their owner for them
  // when the rows change and on a slow tick (kanban counts change without the row changing). Throttled per row.
  const stampRows = useMemo(() => rowsNeedingOwnerStamps(sessions), [sessions])
  const stampRowsRef = useRef(stampRows)
  stampRowsRef.current = stampRows

  useEffect(() => {
    if (stampRows.length) {
      void refreshOwnerRowStamps(stampRows)
    }
  }, [stampRows])

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (stampRowsRef.current.length) {
        void refreshOwnerRowStamps(stampRowsRef.current)
      }
    }, OWNER_STAMP_TICK_MS)

    return () => window.clearInterval(timer)
  }, [])

  const onResumeSession = useCallback(
    (sessionId: string, session?: SessionInfo) => {
      markOpenClick(sessionId, session?.connection_id)
      // [daveey] ui.session_switch: this click is the clock. It covers resuming into main AND fronting a chat
      // that is already a tile (openStoredSession → focusOpenSession); the transcript pane ends it after paint.
      startUiLatency('ui.session_switch', {
        gateway: session?.connection_id,
        profile: session?.profile,
        session_id: sessionId
      })

      if (session?.offline_stale_list) {
        // [daveey] Only its last list failed: the gateway may be busy, not gone. Say so and resume live.
        notify({ durationMs: 6_000, kind: 'warning', message: offlineOpenNotice(session, connectionLabelOf(session)) })
        clearAsleepOpen()
      } else if (session?.offline_cached) {
        // [daveey] Its gateway is unreachable: open the copy cached on this Mac
        // read-only (resumeSession never dials it). Without one, refuse as before.
        if (!hasCachedTranscriptFor(session)) {
          notify({ durationMs: 6_000, kind: 'warning', message: offlineOpenNotice(session) })
          cancelUiLatency('ui.session_switch')

          return
        }

        markAsleepOpen(session)
      } else {
        clearAsleepOpen()
        // [daveey] The cached transcript goes on screen in this click, with the route change (see prepaint).
        prepaintClickedTranscript(sessionId, session)
      }

      resumeSession(sessionId, session)
    },
    [resumeSession]
  )

  return { onResumeSession, sessions }
}

/** Registry label of the connection that owns `session` (rows of the primary backend carry no `connection_id`). */
export function connectionLabelOf(session: Pick<SessionInfo, 'connection_id'>): string | undefined {
  const registry = $connectionsRegistry.get()
  const id = sessionGatewayId(session, registry)

  return registry?.connections.find(connection => connection.id === id)?.label
}
