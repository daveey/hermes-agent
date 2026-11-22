import type { CSSProperties, ReactNode } from 'react'

import { canonicalOutcomeStatus } from '@/lib/session-outcome'
import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { useCommittedStripe } from '@/store/committed-stripe-report'
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
  const stripe = session?.archived ? null : canonicalOutcomeStatus(session?.outcome).stripe
  const reports = Boolean(session) && !session?.archived

  useCommittedStripe(
    { connection_id: session?.connection_id, id: id ?? '', profile: session?.profile },
    session?.outcome,
    'tab',
    stripe,
    reports
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
