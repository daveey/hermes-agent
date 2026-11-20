import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { $sessions, knownSessionOwner, lineageAliases } from '@/store/session'
import { $unavailableWorkSources, workSourceAuthorityComplete } from '@/store/session-work-source'
import {
  $workSourceAuthorityRevision,
  peekWorkSourceAuthorityForSession,
  qualifiedWorkSourceOwner,
  workSourceAuthorityForSession
} from '@/store/work-source-owner'
import type { SessionInfo } from '@/types/hermes'

/** Failed, pending and unread sources veto even an idle stored-done row. */
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

  return !workSourceAuthorityComplete(
    owner.connectionId,
    owner.profile,
    lineageAliases(row.id, sessions),
    authority,
    sources
  )
}
