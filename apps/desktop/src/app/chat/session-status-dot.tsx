import { useStore } from '@nanostores/react'
import type { CSSProperties } from 'react'

import { type Translations, useI18n } from '@/i18n'
import { formatSessionExternalHover } from '@/lib/external-run'
import { unreadDotOutcomeLabel, unreadDotOutcomeStripe } from '@/lib/session-outcome'
import { sessionStatusStripeColor } from '@/lib/session-status-stripe'
import { useExecutionObservationClock } from '@/lib/use-execution-observation-clock'
import { useBackgroundDotLabel, useSessionDotState } from '@/lib/use-session-dot-state'
import { cn } from '@/lib/utils'
import { $sessionColorById, sessionColorFor } from '@/store/session-color'
import type { BackgroundDotLabel, SessionDotState } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'
import type { SessionOutcome } from '@/types/session-outcome'

// A pure lookup table: each state maps to its className, aria-label, and title.
// No priority resolution here — $sessionDotStateById already picked one.
// Label/title resolve from sidebar.row translations, keyed by name.
type DotVariant = {
  ariaLabel?: (r: Translations['sidebar']['row']) => string
  className: string
  role?: 'status'
  style?: CSSProperties
  title?: (r: Translations['sidebar']['row']) => string
}

// Shared base for every active dot; idle is smaller and uses its own class.
const DOT_BASE = 'size-1.5 rounded-full'

// [daveey] Tailwind sky-400 / red-500, the external and external_down stripe hues.
const EXTERNAL_BLUE = '#38bdf8'
const EXTERNAL_RED = '#ef4444'

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
  // Settled: the project color when there is one, else the faintest filled
  // grey. Every session shows SOME mark — a row with nothing in the lead slot
  // reads as broken next to its neighbours, so "no color" falls back to the
  // quietest ink rather than to an invisible dot.
  idle: {
    className: 'size-1 rounded-full bg-(--ui-text-quaternary)'
  }
}

/** The dot a state paints, for surfaces that describe a status rather than
 *  render a session — the sidebar's status filter, say. Idle carries no color
 *  of its own (it inherits the project's), so callers supply one. */
export const sessionDotClassName = (state: SessionDotState): string => DOT_VARIANTS[state].className

/** [daveey] The variant a session's dot paints. Unread is emerald only for a chat that is actually done: a stored
 *  outcome that is waiting / running / unresolved / decision / muted repaints the unread dot in that outcome's stripe
 *  colour (still filled, so it still reads as new), because users read emerald as done. Every other state ignores
 *  the outcome: the live-turn states already outrank it, and idle keeps the project colour.
 *  [daveey] `backgroundLabel`: what the `background` claim rests on. `unknown` (only unknown or unfresh work evidence)
 *  and `waiting` (only known waiting work) keep the same hollow dot but say so rather than asserting "running". */
export const sessionDotVariant = (
  state: SessionDotState,
  outcome?: null | SessionOutcome,
  backgroundLabel: BackgroundDotLabel = 'running'
): DotVariant => {
  if (state === 'background' && backgroundLabel !== 'running') {
    const label = (r: Translations['sidebar']['row']) =>
      backgroundLabel === 'unknown'
        ? r.backgroundUnknown
        : backgroundLabel === 'waiting'
          ? r.backgroundWaiting
          : backgroundLabel === 'external_down'
            ? r.backgroundExternalDown
            : r.backgroundExternal

    // [daveey] External run: filled light blue (down: red), the same hues as the row stripe. Literal colours (not
    // class names) so the paint is the same wherever the dot renders and can be asserted as a computed colour.
    const external = backgroundLabel === 'external' || backgroundLabel === 'external_down'

    return {
      ...DOT_VARIANTS.background,
      ariaLabel: label,
      ...(external
        ? {
            className: DOT_BASE,
            style: { backgroundColor: backgroundLabel === 'external' ? EXTERNAL_BLUE : EXTERNAL_RED }
          }
        : {}),
      title: label
    }
  }

  const stripe = state === 'unread' ? unreadDotOutcomeStripe(outcome) : null

  if (!stripe || !outcome) {
    return DOT_VARIANTS[state]
  }

  return {
    ariaLabel: r => unreadDotOutcomeLabel(outcome, r),
    className: cn(DOT_BASE, 'bg-(--session-status-color)', sessionStatusStripeColor(stripe)),
    role: 'status',
    title: r => unreadDotOutcomeLabel(outcome, r)
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
  /** The session row for color resolution — recents OR the project tree. Both
   *  call sites already hold it; passing it lets the idle dot inherit the
   *  project color even for a session older than the paginated recents page
   *  (which has no `$sessionColorById` entry). */
  session?: null | SessionInfo
  /** TUI-style tree stem for a branched session (`└─ ` / `├─ `). */
  branchStem?: string
  /** Applied to the OUTER wrapper (stem + dot) — e.g. hover-fade on the
   *  reorder handle. */
  className?: string
}

/**
 * SESSION STATUS DOT — the ONE primitive the sidebar row, the pane tabs, and
 * the session switcher render, so a session's status can never disagree
 * between surfaces. It resolves everything itself from the stored session id:
 * the live state (via `$sessionDotStateById`, already reduced to one mutually
 * exclusive answer) and the color (override → project, via `sessionColorFor`).
 * An idle session shows its project color; the active states own the dot with
 * their semantic color so an attention cue is never masked by the tint.
 */
export function SessionStatusDot({ storedSessionId, session, branchStem, className }: SessionStatusDotProps) {
  const { t } = useI18n()
  const r = t.sidebar.row

  // Subscribe to the shared color map for reactivity; sessionColorFor falls
  // back to the resolver for a session outside the recents page.
  useStore($sessionColorById)
  const color = sessionColorFor(session) ?? null

  // Selector, not a plain useStore: the map is rebuilt whenever any session's
  // status changes, but a given dot only repaints when ITS OWN state flips.
  const dotState = useSessionDotState(storedSessionId, session)
  const backgroundLabel = useBackgroundDotLabel(storedSessionId, session, dotState)

  const variant = sessionDotVariant(dotState, session?.outcome, backgroundLabel)

  const nowSeconds = useExecutionObservationClock(session)

  const externalHover =
    backgroundLabel === 'external' || backgroundLabel === 'external_down'
      ? formatSessionExternalHover(
          session?.kanban_external_cards,
          nowSeconds,
          (session?.kanban_external ?? 0) + (session?.kanban_external_down ?? 0)
        )
      : null

  const title = externalHover ?? variant.title?.(r)
  const aria = externalHover ?? variant.ariaLabel?.(r)

  return (
    <span className={cn('flex items-center gap-0.5', className)}>
      {branchStem ? (
        <span aria-hidden className="shrink-0 font-mono text-[0.625rem] leading-none text-(--ui-text-quaternary)">
          {branchStem}
        </span>
      ) : null}
      {dotState === 'idle' ? (
        // Rendered even with no color to paint: an empty dot of the same size
        // keeps every row's title on one left edge, so a session finishing
        // can't shift the list under the pointer.
        <span aria-hidden="true" className={variant.className} style={color ? { backgroundColor: color } : undefined} />
      ) : (
        <span
          aria-label={aria}
          className={variant.className}
          data-background-label={dotState === 'background' ? backgroundLabel : undefined}
          role={variant.role}
          style={variant.style}
          title={title}
        />
      )}
    </span>
  )
}
