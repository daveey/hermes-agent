import { type ReactNode, useContext, useEffect, useState } from 'react'

import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { executionIdentityKey, selectExecutionObservation } from '@/lib/execution-observations'
import { deriveExternalVisual } from '@/lib/external-run'
import { cn } from '@/lib/utils'
import { isWaitKind, ORANGE_OVER, type WaitEta, type WaitReadout, waitReadout } from '@/lib/wait-eta'
import type { SessionStatusStripe } from '@/store/session-dot-state'

import { CardCountsContext } from './card-counts-context'
import { SessionRowCardTimers } from './session-row-card-timers'
import { useWaitClock } from './use-wait-clock'

const COLOURS = {
  red: 'bg-red-500',
  pink: 'bg-pink-500',
  green: 'bg-emerald-500',
  amber: 'bg-amber-500',
  blue: 'bg-sky-400 dark:bg-sky-500',
  grey: 'bg-zinc-400 dark:bg-zinc-500',
  violet: 'bg-violet-500'
} as const

type CardColour = keyof typeof COLOURS

/** Counts do not distinguish ready from running; only current card evidence can refine that fallback. */
function queuedCards(session: SessionInfo): number {
  const values = Array.isArray(session.execution_observations) ? session.execution_observations : []
  const identities = new Map<string, unknown>()

  for (const value of values) {
    if (!value || typeof value !== 'object' || !('identity' in value)) {
      continue
    }

    const identity = value.identity
    const key = executionIdentityKey(identity)

    if (key && identity && typeof identity === 'object' && 'kind' in identity && identity.kind === 'card') {
      identities.set(key, identity)
    }
  }

  const now = Date.now() / 1000

  return [...identities.values()].filter(
    identity => selectExecutionObservation(values, identity, now).state === 'queued'
  ).length
}

/** One entry per distinct card colour, in urgency order. No backend reads. */
export function sessionCardColours(
  session: SessionInfo,
  executionUnknown: number
): { colour: CardColour; count: number }[] {
  const parked = session.kanban_parked ?? 0
  const stalled = session.kanban_stalled ?? 0
  const live = Math.max(0, (session.kanban_active ?? 0) - parked - (session.kanban_held ?? 0))
  const queued = Math.min(live, queuedCards(session))

  const counts: Record<CardColour, number> = {
    red: session.needs_you?.length ?? session.kanban_blocked ?? 0,
    pink: session.kanban_held ?? 0,
    green: stalled + queued,
    amber: session.kanban_attention ?? 0,
    blue: live - queued,
    grey: Math.max(0, parked - stalled) + executionUnknown + Number(Boolean(session.kanban_unknown)),
    violet: session.kanban_unseen ?? 0
  }

  return (Object.keys(COLOURS) as CardColour[]).flatMap(colour =>
    counts[colour] > 0 ? [{ colour, count: counts[colour] }] : []
  )
}

export function aggregateCardColours(sessions: SessionInfo[]): ReturnType<typeof sessionCardColours> {
  const totals = new Map<CardColour, number>()

  for (const session of sessions) {
    for (const { colour, count } of sessionCardColours(session, 0)) {
      totals.set(colour, (totals.get(colour) ?? 0) + count)
    }
  }

  return (Object.keys(COLOURS) as CardColour[]).flatMap(colour => {
    const count = totals.get(colour) ?? 0

    return count > 0 ? [{ colour, count }] : []
  })
}

export const runningCardCount = (session: SessionInfo): number =>
  sessionCardColours(session, 0).find(item => item.colour === 'blue')?.count ?? 0

const trailingColours = (session: SessionInfo, executionUnknown: number) =>
  sessionCardColours(session, executionUnknown).filter(item => item.colour !== 'blue' && item.colour !== 'grey')

export function SessionRowCardSquares({
  session,
  executionUnknown
}: {
  session: SessionInfo
  executionUnknown: number
}) {
  const counts = useContext(CardCountsContext)
  const colours = counts ? sessionCardColours(session, 0) : trailingColours(session, executionUnknown)

  return <CardSquares colours={colours} counts={counts} />
}

export function CardSquares({
  colours,
  counts = false
}: {
  colours: ReturnType<typeof sessionCardColours>
  counts?: boolean
}) {
  const { t } = useI18n()

  if (!colours.length) {
    return null
  }

  const label = colours.map(({ colour, count }) => `${t.sidebar.row.cardColours[colour]} ${count}`).join(' · ')

  return (
    <Tip label={label} side="top">
      <span
        aria-label={label}
        className="pointer-events-auto ml-1 inline-flex items-center gap-[2px] align-middle"
        data-testid="session-row-card-squares"
        role="img"
      >
        {colours.map(({ colour, count }) =>
          counts ? (
            <span className="inline-flex items-center gap-[2px]" data-card-colour={colour} key={colour}>
              <span aria-hidden="true" className={`size-[6px] shrink-0 rounded-[1.5px] ${COLOURS[colour]}`} />
              <span className="text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">{count}</span>
            </span>
          ) : (
            <span
              aria-hidden="true"
              className={`size-[6px] shrink-0 rounded-[1.5px] ${COLOURS[colour]}`}
              data-card-colour={colour}
              key={colour}
            />
          )
        )}
      </span>
    </Tip>
  )
}

/** Keep the gateway marks last, and don't displace a slot-ending opt-in PR chip on empty rows. On a light-blue row
 *  (CI / deploy / timer wait) the ETA readout replaces the squares. */
export function useFlatRowCardMarks(
  session: SessionInfo,
  executionUnknown: number,
  gatewayIcon: ReactNode,
  statusStripe?: null | SessionStatusStripe
): ReactNode {
  return flatRowCardMarks(session, executionUnknown, gatewayIcon, statusStripe, useContext(CardCountsContext))
}

export function flatRowCardMarks(
  session: SessionInfo,
  executionUnknown: number,
  gatewayIcon: ReactNode,
  statusStripe?: null | SessionStatusStripe,
  counts = false
): ReactNode {
  const readouts = !counts && statusStripe === 'external' ? sessionWaitEtas(session, Date.now() / 1000) : []
  const colours = counts ? sessionCardColours(session, 0) : trailingColours(session, executionUnknown)

  if (!gatewayIcon && !readouts.length && !colours.length && !session.kanban_timed_cards?.length) {
    return null
  }

  return (
    <span className="pointer-events-none whitespace-nowrap text-[0.625rem] leading-none text-(--ui-text-tertiary)">
      {readouts.length ? <SessionRowEtaReadout etas={readouts} /> : <CardSquares colours={colours} counts={counts} />}
      <SessionRowCardTimers session={session} />
      {gatewayIcon}
    </span>
  )
}

/** `<waited> / <expected>  <left>` in aligned tabular columns; the soonest-ending wait leads, the rest are in the title. */
export function SessionRowEtaReadout({ etas }: { etas: WaitEta[] }) {
  const [fast, setFast] = useState(false)
  const now = useWaitClock(fast)
  const readouts = etas.map(eta => waitReadout(eta, now)).filter((r): r is WaitReadout => r !== null)
  const [lead] = [...readouts].sort(byLeft)

  const nearMinute = lead !== undefined && Math.min(lead.waitedS, Math.abs(lead.leftS ?? Infinity)) < 60

  useEffect(() => {
    setFast(nearMinute)
  }, [nearMinute])

  if (!lead) {
    return null
  }

  const title = readouts.map(r => r.title).join('\n')

  return (
    <Tip label={<span className="whitespace-pre-line">{title}</span>} side="top">
      <span
        aria-label={title}
        className="pointer-events-auto ml-1 inline-grid grid-cols-[1.75rem_2.125rem_2rem] items-baseline gap-x-[2px] text-right align-middle tabular-nums"
        data-over={lead.over ? 'true' : undefined}
        data-testid="session-row-eta"
        role="img"
      >
        <span className="opacity-75 text-(--ui-text-secondary)" data-eta="waited">
          {lead.waited}
        </span>
        <span className="pl-0.5 text-left" data-eta="expected">
          / {lead.expected ?? '?'}
        </span>
        <span
          className={lead.unknown ? undefined : cn('font-medium', !lead.over && 'text-sky-400')}
          data-eta="left"
          style={lead.over ? { color: ORANGE_OVER } : undefined}
        >
          {lead.left}
        </span>
      </span>
    </Tip>
  )
}

const byLeft = (a: WaitReadout, b: WaitReadout): number => (a.leftS ?? Infinity) - (b.leftS ?? Infinity)

/** The ETA of every light-blue wait the row carries (CI / deploy / timer cards that are not down). */
export function sessionWaitEtas(session: SessionInfo, now: number): WaitEta[] {
  return (session.kanban_external_cards ?? []).flatMap(card =>
    card.eta && isWaitKind(card.eta.wait_kind) && deriveExternalVisual(card.external, now) !== 'down' ? [card.eta] : []
  )
}
