import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { reportSessionStripeRemote, type SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { outcomeHasCrons, outcomeHasWatches, outcomeStripe, outcomeTooltip } from '@/lib/session-outcome'
import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { useExecutionObservationClock } from '@/lib/use-execution-observation-clock'
import { useSessionParked } from '@/lib/use-session-parked'
import { useWorkSourceUnavailable } from '@/lib/use-work-source-unavailable'
import { cn } from '@/lib/utils'
import { stuckOnPausedQueue } from '@/store/owner-queue-landed'
import { $pausedQueueSessionIds } from '@/store/owner-queue-reconcile'
import { $sessions } from '@/store/session'
import {
  $stuckPromptInputsKey,
  kanbanStripeCounts,
  outcomePaintsStripe,
  recordStripeTransition,
  type SessionDotState,
  sessionStatusStripe,
  type SessionStatusStripe,
  setStripeReasonReporter
} from '@/store/session-dot-state'

import { sessionExternalStripeTip, unknownExternalCloud } from './session-row-stripe-external'

// [daveey] Every red edge (with its reasons) also goes to the session's own gateway, where the agent can read it.
setStripeReasonReporter((row, stripe, previous, reasons) =>
  reportSessionStripeRemote(
    { connection_id: row.connection_id, id: row.id, profile: row.profile },
    stripe,
    previous,
    reasons
  )
)

/** What a row that is not `needs-input` subscribes to instead of `$stuckPromptInputsKey`: it never changes. */
const NO_PROMPT_INPUTS = atom('')

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
  // Make time an explicit input so React Compiler cannot cache counts across evidence expiry.
  const nowSeconds = useExecutionObservationClock(session)
  const counts = kanbanStripeCounts(session, nowSeconds)
  const sourceUnavailable = useWorkSourceUnavailable(session)

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
        session.outcome,
        sourceUnavailable,
        counts.external,
        counts.externalDown
      )
    : null

  // [daveey] desktop.log trace of every stripe change with the row inputs that fed it (`stripe-transition <sid>`),
  // so the next "green while work is queued" report has the payload. Change-only and capped inside. A red row whose
  // prompt moved to another runtime (or whose paused queue card changed) has new reasons with the same stripe and
  // dot, so the prompt inputs are a dependency too: otherwise that new red is never re-reported. Only a `needs-input`
  // row's reasons read them, so every other row subscribes to a constant and skips the re-render when prompts change.
  const promptInputsKey = useStore(dotState === 'needs-input' ? $stuckPromptInputsKey : NO_PROMPT_INPUTS)

  useEffect(() => {
    recordStripeTransition(session, statusStripe, dotState, Date.now(), live)
  }, [session, statusStripe, dotState, promptInputsKey, live])

  // Only explain the stripe with the outcome when it actually painted it: open asks and unfinished work override it.
  const outcomePainted =
    outcomePaintsStripe(dotState, userBlocked) && statusStripe === outcomeStripe(session.outcome)

  return { outcomePainted, statusStripe, userBlocked }
}

/** [daveey] Stripes drawn as a small glyph in the stripe's place: a cron loop, or the card glyph for kanban waits. */
const GLYPH_STRIPES: ReadonlySet<SessionStatusStripe> = new Set([
  'external',
  'external_down',
  'periodic',
  'stalled',
  'waiting'
])

interface SessionRowStatusStripeProps {
  session: SessionInfo
  statusStripe: null | SessionStatusStripe
  /** A ⌘B park or a live turn beats the outcome, so its tooltip only shows while the outcome painted the stripe. */
  outcomePainted: boolean
}

/** [daveey] Which glyphs the stripe draws: `sync` for a live cron or a done chat's owned cron, `project` for open
 *  cards, plus the eye for a done chat still watching a card (self-heal desktop-e6cce5965c). */
function stripeGlyphs(statusStripe: SessionStatusStripe, painted: boolean, outcome: SessionInfo['outcome']) {
  const doneCron = painted && outcomeHasCrons(outcome)
  const doneWatch = painted && outcomeHasWatches(outcome)
  const listed = GLYPH_STRIPES.has(statusStripe)

  const mainGlyph =
    statusStripe === 'periodic' || doneCron
      ? 'sync'
      : statusStripe === 'external' || statusStripe === 'external_down'
        ? 'cloud'
        : listed
          ? 'project'
          : null

  return { doneWatch, glyph: listed || doneCron || doneWatch, mainGlyph }
}

/** [daveey] The stripe itself, with the stored outcome's explanation on hover: the summary, the open questions, what
 *  it waits on, or why it is unresolved. */
export function SessionRowStatusStripe({ session, statusStripe, outcomePainted }: SessionRowStatusStripeProps) {
  const { t } = useI18n()
  const pausedIds = useStore($pausedQueueSessionIds)
  const nowSeconds = useExecutionObservationClock(session)

  if (!statusStripe) {
    return null
  }

  const outcomeKind = outcomePainted && outcomeStripe(session.outcome) ? session.outcome?.kind : undefined
  const pausedQueue = statusStripe === 'stuck' && stuckOnPausedQueue(session.id, pausedIds, $sessions.get())

  const tipText = pausedQueue
    ? t.sidebar.row.pausedQueue
    : outcomeKind
      ? outcomeTooltip(session.outcome, t.sidebar.row)
      : sessionExternalStripeTip(session, statusStripe, nowSeconds)

  const unknownCloud = unknownExternalCloud(session, statusStripe, nowSeconds)

  const { doneWatch, glyph, mainGlyph } = stripeGlyphs(statusStripe, Boolean(outcomeKind), session.outcome)

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
        data-external-unknown={unknownCloud ? 'true' : undefined}
        data-outcome={outcomeKind}
        data-slot="session-status-stripe"
        data-status-stripe={statusStripe}
        role={tipText ? 'img' : undefined}
      >
        {/* `periodic`: a live cron job, it recurs so it is never "done". `waiting`: open kanban cards hold it — the
            card glyph (amber when `stalled`). A finished chat's cron keeps the loop glyph, in the green of `done`.
            [daveey] A finished chat still watching a card adds the eye, so it never reads as "all done". */}
        {glyph && mainGlyph && (
          <span
            className={
              unknownCloud
                ? '[-webkit-text-stroke:0.05em_currentColor] [-webkit-text-fill-color:transparent]'
                : undefined
            }
            data-external-cloud={unknownCloud ? 'hollow' : 'solid'}
          >
            <Codicon name={mainGlyph} size="0.5625rem" />
          </span>
        )}
        {doneWatch && (
          <span className="flex" data-testid="stripe-watch">
            <Codicon name="eye" size="0.5625rem" />
          </span>
        )}
      </span>
    </Tip>
  )
}
