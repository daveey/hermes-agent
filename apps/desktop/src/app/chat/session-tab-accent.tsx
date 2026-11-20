import type { CSSProperties, ReactNode } from 'react'

import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { useExecutionObservationClock } from '@/lib/use-execution-observation-clock'
import { useSessionDotState } from '@/lib/use-session-dot-state'
import { useSessionParked } from '@/lib/use-session-parked'
import { useWorkSourceUnavailable } from '@/lib/use-work-source-unavailable'
import { kanbanStripeCounts, sessionStatusStripe } from '@/store/session-dot-state'
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
  const state = useSessionDotState(id, session)
  const blocked = useSessionParked(id, session)

  const nowSeconds = useExecutionObservationClock(session)
  const counts = kanbanStripeCounts(session ?? {}, nowSeconds)
  const sourceUnavailable = useWorkSourceUnavailable(session)

  const stripe = session?.archived
    ? null
    : sessionStatusStripe(
        state,
        (session?.message_count ?? 0) > 0,
        blocked,
        counts.blocked,
        session?.cron_active,
        session?.review_wait,
        session?.kanban_attention,
        counts.waiting,
        counts.stalled,
        session?.outcome,
        sourceUnavailable,
        counts.external,
        counts.externalDown
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
