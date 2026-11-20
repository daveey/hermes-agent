import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { $sessions, knownSessionOwner, lineageAliases } from '@/store/session'
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
  const sessions = useStore($sessions)
  useStore($workSourceAuthorityRevision)

  const owner = row ? qualifiedWorkSourceOwner(knownSessionOwner([row], row.id)) : undefined
  const connectionId = owner?.connectionId
  const profile = owner?.profile
  const storedSessionId = row?.id

  useEffect(() => {
    if (connectionId && profile && storedSessionId) {
      workSourceAuthorityForSession(connectionId, profile, storedSessionId)
    }
  }, [connectionId, profile, storedSessionId])

  if (!row) {
    return false
  }

  if (!owner) {
    return true
  }

  const authority = peekWorkSourceAuthorityForSession(owner.connectionId, owner.profile, row.id)
  const ids = lineageAliases(row.id, sessions)

  // The displayed chat gets every source read, so it must earn green by positive proof (a rebind revokes it).
  if (workSourceSessionDisplayed(owner.connectionId, owner.profile, row.id)) {
    return !workSourceAuthorityComplete(owner.connectionId, owner.profile, ids, authority, sources)
  }

  return workSourceVetoed(owner.connectionId, owner.profile, ids, authority, sources)
}
