import type { CSSProperties } from 'react'

import { type Translations, useI18n } from '@/i18n'
import { canonicalOutcomeStatus } from '@/lib/session-outcome'
import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { cn } from '@/lib/utils'
import { useCommittedStripe } from '@/store/committed-stripe-report'
import type { BackgroundDotLabel, SessionDotState } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'
import type { SessionOutcome } from '@/types/session-outcome'

// A pure lookup table: each state maps to its className, aria-label, and title.
// Live priority comes from $sessionDotStateById; sidebar card activity only promotes passive states.
// Label/title resolve from sidebar.row translations, keyed by name.
type DotVariant = {
  ariaLabel?: (r: Translations['sidebar']['row']) => string
  className: string
  role?: 'status'
  style?: CSSProperties
  title?: (r: Translations['sidebar']['row']) => string
}

// Shared size for every dot, including settled sessions.
const DOT_BASE = 'size-1.5 rounded-full'

// Three colors and one fill/hollow axis, none of it moving. Motion on a 6px
// circle can only say "something is happening" — which the row's arc already
// says, better — while costing a repaint per frame on every row at once. What
// the dot is for is telling states APART, and that is a job for color and fill:
// filled means producing, hollow means open but quiet. The two states this
// replaces differed by 30% opacity and were, in practice, the same dot.
const DOT_VARIANTS: Record<SessionDotState, DotVariant> = {
  // Amber — a clarify/approval is blocking the turn. The one "act now" color,
  // and the only state the user is required to do something about.
  'needs-input': {
    ariaLabel: r => r.needsInput,
    className: `${DOT_BASE} bg-amber-500`,
    role: 'status',
    title: r => r.waitingForAnswer
  },
  // Accent — the turn is running. The row's arc carries the motion.
  working: {
    ariaLabel: r => r.sessionRunning,
    className: `${DOT_BASE} bg-(--ui-accent)`,
    role: 'status'
  },
  // Hollow accent — still authoritatively running, but nothing has arrived for
  // the watchdog window. Same color as working because it IS working; hollow
  // because nothing is coming out of it right now.
  stalled: {
    ariaLabel: r => r.sessionRunning,
    className: `${DOT_BASE} border border-(--ui-accent)`,
    role: 'status',
    title: r => r.sessionRunning
  },
  // Hollow muted — a terminal(background=true) process outlived the turn. An
  // outline reads as "still open" without claiming the model is working; a
  // filled grey dot read as finished, the opposite of what this means.
  background: {
    ariaLabel: r => r.backgroundRunning,
    className: `${DOT_BASE} border border-(--ui-text-tertiary)`,
    role: 'status',
    title: r => r.backgroundRunning
  },
  // Emerald — the turn finished while the user was looking elsewhere. The
  // color is theme-derived (`--ui-success`, a success green rotated toward the
  // accent) so eight finished dots can't sit in the sidebar fighting a palette
  // they don't belong to. Under a green accent it stays emerald.
  unread: {
    ariaLabel: r => r.finishedUnread,
    className: `${DOT_BASE} bg-(--ui-success)`,
    role: 'status',
    title: r => r.finishedUnread
  },
  // Hollow grey, the faintest ink the app has — nothing has ever run here. It
  // shares the outline with `background` because both mean "open, not
  // producing", and sits a shade dimmer because a draft is the one state that
  // has yet to do anything at all.
  draft: {
    ariaLabel: r => r.draftSession,
    className: `${DOT_BASE} border border-(--ui-text-quaternary)`,
    title: r => r.draftSession
  },
  // Settled: a hollow grey circle, independent of the project's colour.
  idle: {
    className: `${DOT_BASE} border border-(--ui-text-quaternary)`
  }
}

/** The dot a state paints, for surfaces that describe a status rather than
 *  render a session — the sidebar's status filter, say. */
export const sessionDotClassName = (state: SessionDotState): string => DOT_VARIANTS[state].className

/** Every session dot consumes the same backend status as the row and tab. Activity is not status authority. */
export const sessionDotVariant = (
  _state: SessionDotState,
  outcome?: null | SessionOutcome,
  _backgroundLabel: BackgroundDotLabel = 'running'
): DotVariant => {
  const status = canonicalOutcomeStatus(outcome)
  const stripe = status.stripe

  const label = (r: Translations['sidebar']['row']) =>
    status.status === 'unknown'
      ? r.backgroundUnknown
      : stripe === 'done'
        ? r.outcomeDone
        : stripe === 'stuck'
          ? outcome?.status?.presentation?.reason_codes?.includes('triage_overdue') ? r.outcomeTriageOverdue : r.outcomeDecision
          : stripe === 'blocked'
            ? r.outcomeMuted
            : stripe === 'attention'
              ? r.outcomeUnresolved(null)
              : stripe === 'external'
                ? r.outcomeWaiting
                : stripe === 'waiting'
                  ? outcome?.status?.kind === 'running'
                    ? r.outcomeRunning
                    : r.outcomeWaiting
                  : r.draftSession

  return {
    ariaLabel: label,
    className: cn(
      DOT_BASE,
      stripe === 'done'
        ? 'bg-(--ui-success)'
        : stripe === 'waiting' && outcome?.status?.kind === 'running'
          ? 'bg-(--ui-accent)'
          : stripe
            ? 'bg-(--session-status-color)'
            : 'border border-(--ui-text-quaternary)',
      sessionStatusStripeColor(stripe)
    ),
    role: 'status',
    style: stripe === 'external' ? { backgroundColor: '#38bdf8' } : undefined,
    title: label
  }
}

export interface SessionStatusDotProps {
  /** The STORED session id — the key every live-state atom (working /
   *  attention / stalled / unread / background) is keyed by, on BOTH surfaces:
   *  the sidebar row's `session.id` and a pane tile's `storedSessionId` are the
   *  same stored id (`$workingSessionIds` et al. map `storedSessionId`).
   *
   *  Null on a new chat that has yet to reach the backend — no id to key by,
   *  and no turn behind it, which is the draft state by definition. */
  storedSessionId: null | string
  /** The session row supplies outcome and execution evidence on every surface. */
  session?: null | Pick<SessionInfo, 'id' | 'profile' | 'connection_id' | 'outcome' | 'archived' | 'kanban_unknown'>
  /** Sidebar-only activity; other surfaces retain their agent-only presentation. */
  runningCardCount?: number
  /** TUI-style tree stem for a branched session (`└─ ` / `├─ `). */
  branchStem?: string
  /** Applied to the OUTER wrapper (stem + dot) — e.g. hover-fade on the
   *  reorder handle. */
  className?: string
}

/**
 * SESSION STATUS DOT — the ONE primitive the sidebar row, the pane tabs, and
 * the session switcher render, so a session's status can never disagree
 * between surfaces. It resolves the live state via `$sessionDotStateById`,
 * already reduced to one mutually exclusive answer. Idle is hollow grey;
 * active states own the dot with their semantic colour.
 */
export function SessionStatusDot({ storedSessionId, session, branchStem, className }: SessionStatusDotProps) {
  const { t } = useI18n()
  const r = t.sidebar.row

  // Missing card observations may not certify passive waiting/idle/done. They do
  // not erase an independently published own turn or a human decision.
  const outcome =
    session?.kanban_unknown && !['running', 'decision', 'muted'].includes(session.outcome?.status?.kind ?? '')
      ? undefined
      : session?.outcome

  const status = canonicalOutcomeStatus(outcome)
  const variant = sessionDotVariant('idle', outcome)

  useCommittedStripe(
    { connection_id: session?.connection_id, id: session?.id ?? storedSessionId ?? '', profile: session?.profile },
    session?.outcome,
    'dot',
    status.stripe,
    Boolean(session) && !session?.archived
  )

  return (
    <span className={cn('flex items-center gap-0.5', className)}>
      {branchStem ? (
        <span aria-hidden className="shrink-0 font-mono text-[0.625rem] leading-none text-(--ui-text-quaternary)">
          {branchStem}
        </span>
      ) : null}
      <span
        aria-label={variant.ariaLabel?.(r)}
        className={variant.className}
        data-session-status-dot={status.stripe ?? 'none'}
        role={variant.role}
        style={variant.style}
        title={variant.title?.(r)}
      />
    </span>
  )
}
