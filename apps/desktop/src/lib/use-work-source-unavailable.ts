import { useStore } from '@nanostores/react'

import { $sessions, lineageAliases } from '@/store/session'
import { $unavailableWorkSources, workSourceUnavailable } from '@/store/session-work-source'
import type { SessionInfo } from '@/types/hermes'

/** Subscribe independently of focus/turn state: failed reads must still veto an idle row. */
export function useWorkSourceUnavailable(row: SessionInfo | null | undefined): boolean {
  const sources = useStore($unavailableWorkSources)
  const sessions = useStore($sessions)

  return row ? workSourceUnavailable(row.connection_id, row.profile, lineageAliases(row.id, sessions), sources) : false
}
