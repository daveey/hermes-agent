import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { $projectTree } from '@/store/project-tree'
import {
  $cronSessions,
  $messagingSessions,
  $sessions,
  $unlistedSessionOwnerRows,
  knownSessionOwner,
  lineageAliases,
  ownerLookupSessionRows
} from '@/store/session'
import { $unavailableWorkSources, workSourceAuthorityComplete, workSourceVetoed } from '@/store/session-work-source'
import {
  $workSourceAuthorityRevision,
  peekWorkSourceAuthorityForSession,
  qualifiedWorkSourceOwner,
  workSourceAuthorityForSession,
  workSourceSessionDisplayed
} from '@/store/work-source-owner'
import type { SessionInfo } from '@/types/hermes'

/** The displayed chat needs every work source positively read for its current authority; any other row is vetoed
 *  only by a pending or failed read of its current authority. Process / subagent / control are read only for the
 *  displayed chat, so demanding proof everywhere painted every other finished chat grey, green only once opened. */
export function useWorkSourceUnavailable(row: SessionInfo | null | undefined): boolean {
  'use no memo'
  const sources = useStore($unavailableWorkSources)
  // A row may live only in the cron / messaging slice or the project tree: its lineage comes from every loaded row.
  useStore($sessions)
  useStore($cronSessions)
  useStore($messagingSessions)
  useStore($projectTree)
  useStore($unlistedSessionOwnerRows)
  useStore($workSourceAuthorityRevision)

  const owner = row ? qualifiedWorkSourceOwner(knownSessionOwner([row], row.id)) : undefined
  const connectionId = owner?.connectionId
  const profile = owner?.profile
  const ids = row ? rowLineage(row) : []
  // Every lineage member is tracked: the runtime on screen and reads may hold any of them (compression rotates the
  // tip), and only a tracked id gets an authority epoch and runtime roles that republish the revision on change.
  const trackedIds = ids.join('\u0000')

  useEffect(() => {
    if (connectionId && profile && trackedIds) {
      for (const id of trackedIds.split('\u0000')) {
        workSourceAuthorityForSession(connectionId, profile, id)
      }
    }
  }, [connectionId, profile, trackedIds])

  if (!row) {
    return false
  }

  if (!owner) {
    return true
  }

  const authority = peekWorkSourceAuthorityForSession(owner.connectionId, owner.profile, row.id)

  // The displayed chat gets every source read, so it must earn green by positive proof (a rebind revokes it). Its
  // runtime may hold any member of the lineage (compression rotates the tip), so any alias on screen counts.
  if (ids.some(id => workSourceSessionDisplayed(owner.connectionId, owner.profile, id))) {
    return !workSourceAuthorityComplete(owner.connectionId, owner.profile, ids, authority, sources)
  }

  return workSourceVetoed(owner.connectionId, owner.profile, ids, authority, sources, id =>
    peekWorkSourceAuthorityForSession(owner.connectionId, owner.profile, id)
  )
}

/** A row in no loaded list gets no tracked authority, so the no-live-authority veto holds it as before. */
function rowLineage(row: SessionInfo): string[] {
  return lineageAliases(row.id, ownerLookupSessionRows())
}
