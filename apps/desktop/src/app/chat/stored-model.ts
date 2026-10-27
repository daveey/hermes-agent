import { useStore } from '@nanostores/react'

import type { SessionView } from '@/app/chat/session-view'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

export interface StoredModelSelection {
  model: string
  provider: string
}

const NONE: StoredModelSelection = { model: '', provider: '' }

/** The model/provider a stored chat last ran on, straight off its sessions
 *  row (sidebar list). Empty for a draft or a row the list has not loaded. */
export function storedModelSelection(
  sessions: readonly Pick<SessionInfo, '_lineage_ids' | '_lineage_root_id' | 'id' | 'model' | 'model_provider'>[],
  storedId: null | string
): StoredModelSelection {
  if (!storedId) {
    return NONE
  }

  const row = sessions.find(session => sessionMatchesStoredId(session, storedId))

  if (!row?.model) {
    return NONE
  }

  return { model: row.model, provider: row.model_provider || '' }
}

/**
 * THIS surface's stored model. While a chat's agent is not loaded its
 * SessionView carries no model; the composer falls back to this instead of a
 * bare spinner (pill) or the profile default's capabilities (effort menu).
 */
export function useStoredModelSelection(view: SessionView): StoredModelSelection {
  const storedId = useStore(view.$storedId)
  const sessions = useStore($sessions)

  return storedModelSelection(sessions, storedId)
}
