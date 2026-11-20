import { useStore } from '@nanostores/react'

import { $sessions, knownSessionOwner, lineageAliases } from '@/store/session'
import { $unavailableWorkSources, workSourceAuthorityComplete } from '@/store/session-work-source'
import {
  $workSourceAuthorityRevision,
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

  if (!row) {
    return false
  }
  const owner = qualifiedWorkSourceOwner(knownSessionOwner([row], row.id))

  if (!owner) {
    return true
  }
  const authority = workSourceAuthorityForSession(owner.connectionId, owner.profile, row.id)

  return !workSourceAuthorityComplete(
    owner.connectionId,
    owner.profile,
    lineageAliases(row.id, sessions),
    authority,
    sources
  )
}
