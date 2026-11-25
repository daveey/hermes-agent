import { createContext, useContext } from 'react'

import type { SessionInfo } from '@/hermes'
import { sessionTitle } from '@/lib/chat-runtime'

/** Only Background opts into numeric card marks, including running cards. */
export const CardCountsContext = createContext(false)

/** Presentation only: retain the original row for search, callbacks and persistence. */
export function useSessionTitle(session: SessionInfo) {
  const background = useContext(CardCountsContext)
  const title = sessionTitle(session)

  return { title, label: background ? title.replace(/^Kanban owner:\s*/, '') : title }
}
