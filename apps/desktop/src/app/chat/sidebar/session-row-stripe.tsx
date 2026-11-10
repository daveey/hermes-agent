import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { outcomeHasCrons, outcomeStripe, outcomeTooltip } from '@/lib/session-outcome'
import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { useSessionParked } from '@/lib/use-session-parked'
import { cn } from '@/lib/utils'
import { stuckOnPausedQueue } from '@/store/owner-queue-landed'
import { $pausedQueueSessionIds } from '@/store/owner-queue-reconcile'
import { $sessions } from '@/store/session'
import {
  kanbanStripeCounts,
  outcomePaintsStripe,
  recordStripeTransition,
  type SessionDotState,
  sessionStatusStripe,
  type SessionStatusStripe
} from '@/store/session-dot-state'

/**
 * [daveey] The sidebar row's left stripe: the session's status. Green = its turn is done and waiting on you (stays
 * after you open it); red = not done but stuck (blocked on a prompt, gone quiet mid-turn, kanban cards it made are
 * blocked on a human, or a PR it links waits on a review that's on you). Working: no stripe. A review only others
 * were asked for (<24h) is grey. ⌘B parks a session as blocked: grey stripe, skipped by ⌘] / ⌘[. The park is the
 * backend's `row.muted`; the local ⌘B list is its optimistic cache. A stored `row.outcome` paints it when present.
 */
export function useSessionRowStripe(
  session: SessionInfo,
  dotState: SessionDotState,
  live: boolean
): { outcomePainted: boolean; statusStripe: null | SessionStatusStripe; userBlocked: boolean } {
  const userBlocked = useSessionParked(session.id, session)
  const counts = kanbanStripeCounts(session)

  const statusStripe = live
    ? sessionStatusStripe(
        dotState,
        session.message_count > 0,
        userBlocked,
        counts.blocked,
        session.cron_active === true,
        session.review_wait,
        session.kanban_attention,
        counts.waiting,
        counts.stalled,
        session.outcome
      )
    : null

  // [daveey] desktop.log trace of every stripe change with the row inputs that fed it (`stripe-transition <sid>`),
  // so the next "green while work is queued" report has the payload. Change-only and capped inside.
  useEffect(() => {
    recordStripeTransition(session, statusStripe, dotState)
  }, [session, statusStripe, dotState])

  return { outcomePainted: outcomePaintsStripe(dotState, userBlocked), statusStripe, userBlocked }
}

/** [daveey] Stripes drawn as a small glyph in the stripe's place: a cron loop, or the card glyph for kanban waits. */
const GLYPH_STRIPES: ReadonlySet<SessionStatusStripe> = new Set(['periodic', 'stalled', 'waiting'])

interface SessionRowStatusStripeProps {
  session: SessionInfo
  statusStripe: null | SessionStatusStripe
  /** A ⌘B park or a live turn beats the outcome, so its tooltip only shows while the outcome painted the stripe. */
  outcomePainted: boolean
}

/** [daveey] The stripe itself, with the stored outcome's explanation on hover: the summary, the open questions, what
 *  it waits on, or why it is unresolved. */
export function SessionRowStatusStripe({ session, statusStripe, outcomePainted }: SessionRowStatusStripeProps) {
  const { t } = useI18n()
  const pausedIds = useStore($pausedQueueSessionIds)

  if (!statusStripe) {
    return null
  }

  const outcomeKind = outcomePainted && outcomeStripe(session.outcome) ? session.outcome?.kind : undefined
  const pausedQueue = statusStripe === 'stuck' && stuckOnPausedQueue(session.id, pausedIds, $sessions.get())

  // [daveey] A red painted by this app's own queue names its cause: the backend's status says nothing about it.
  const tipText = pausedQueue
    ? t.sidebar.row.pausedQueue
    : outcomeKind
      ? outcomeTooltip(session.outcome, t.sidebar.row)
      : null

  const doneCron = Boolean(outcomeKind) && outcomeHasCrons(session.outcome)
  const glyph = GLYPH_STRIPES.has(statusStripe) || doneCron

  return (
    <Tip label={tipText ? <span className="whitespace-pre-line">{tipText}</span> : null}>
      <span
        aria-hidden={tipText ? undefined : 'true'}
        aria-label={tipText ?? undefined}
        className={cn(
          glyph
            ? 'absolute top-1/2 -left-0.5 flex -translate-y-1/2 text-(--session-status-color)'
            : 'absolute top-1 bottom-1 left-0 w-[3px] rounded-full bg-(--session-status-color)',
          tipText ? 'pointer-events-auto' : 'pointer-events-none',
          sessionStatusStripeColor(statusStripe)
        )}
        data-outcome={outcomeKind}
        data-slot="session-status-stripe"
        data-status-stripe={statusStripe}
        role={tipText ? 'img' : undefined}
      >
        {/* `periodic`: a live cron job, it recurs so it is never "done". `waiting`: open kanban cards hold it — the
            card glyph (amber when `stalled`). A finished chat's cron keeps the loop glyph, in the green of `done`. */}
        {glyph && <Codicon name={statusStripe === 'periodic' || doneCron ? 'sync' : 'project'} size="0.5625rem" />}
      </span>
    </Tip>
  )
}
