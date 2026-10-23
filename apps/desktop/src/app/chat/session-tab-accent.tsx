import type { CSSProperties, ReactNode } from 'react'

import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { useStoreSelector } from '@/lib/use-session-slice'
import { $blockedSessionIds, isSessionBlocked } from '@/store/session-blocked'
import { $sessionDotStateById, sessionStatusStripe } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

interface SessionTabAccentProps {
  children: ReactNode
  session?: null | SessionInfo
  storedSessionId: null | string
}

/** Scope the existing PaneTab accent to session tabs only; draft/running tabs
 *  use neutral grey while non-session tabs keep the theme-primary fallback. */
export function SessionTabAccent({ children, session, storedSessionId }: SessionTabAccentProps) {
  const id = session?.id ?? storedSessionId
  const state = useStoreSelector($sessionDotStateById, states => (id ? (states[id] ?? 'idle') : 'draft'))
  const blocked = useStoreSelector($blockedSessionIds, ids => (id ? isSessionBlocked(id, ids) : false))

  const stripe = session?.archived
    ? null
    : sessionStatusStripe(
        state,
        (session?.message_count ?? 0) > 0,
        blocked,
        session?.kanban_blocked,
        session?.cron_active,
        session?.review_wait,
        session?.kanban_attention
      )

  return (
    <span
      className={`contents ${sessionStatusStripeColor(stripe)}`}
      data-session-tab-accent={stripe ?? 'neutral'}
      style={{ '--pane-tab-active-accent': 'var(--session-status-color)' } as CSSProperties}
    >
      {children}
    </span>
  )
}
