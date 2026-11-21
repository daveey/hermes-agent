import type { ReactNode } from 'react'

import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'

const COLOURS = {
  red: 'bg-red-500',
  pink: 'bg-pink-500',
  amber: 'bg-amber-500',
  blue: 'bg-sky-400 dark:bg-sky-500',
  grey: 'bg-zinc-400 dark:bg-zinc-500',
  green: 'bg-emerald-500'
} as const

type CardColour = keyof typeof COLOURS

/** One entry per distinct card colour, in urgency order. No backend reads. */
export function sessionCardColours(
  session: SessionInfo,
  executionUnknown: number
): { colour: CardColour; count: number }[] {
  const parked = session.kanban_parked ?? 0
  const stalled = session.kanban_stalled ?? 0

  const counts: Record<CardColour, number> = {
    red: session.needs_you?.length ?? session.kanban_blocked ?? 0,
    pink: session.kanban_held ?? 0,
    amber: stalled + (session.kanban_attention ?? 0),
    blue: Math.max(0, (session.kanban_active ?? 0) - parked - (session.kanban_held ?? 0)),
    grey: Math.max(0, parked - stalled) + executionUnknown + Number(Boolean(session.kanban_unknown)),
    green: session.kanban_unseen ?? 0
  }

  return (Object.keys(COLOURS) as CardColour[]).flatMap(colour =>
    counts[colour] > 0 ? [{ colour, count: counts[colour] }] : []
  )
}

export function SessionRowCardSquares({
  session,
  executionUnknown
}: {
  session: SessionInfo
  executionUnknown: number
}) {
  const { t } = useI18n()
  const colours = sessionCardColours(session, executionUnknown)

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
        {colours.map(({ colour }) => (
          <span
            aria-hidden="true"
            className={`size-[6px] shrink-0 rounded-[1.5px] ${COLOURS[colour]}`}
            data-card-colour={colour}
            key={colour}
          />
        ))}
      </span>
    </Tip>
  )
}

/** Keep the gateway marks last, and don't displace a slot-ending opt-in PR chip on empty rows. */
export function flatRowCardMarks(session: SessionInfo, executionUnknown: number, gatewayIcon: ReactNode): ReactNode {
  if (!gatewayIcon && !sessionCardColours(session, executionUnknown).length) {
    return null
  }

  return (
    <span className="pointer-events-none whitespace-nowrap text-[0.625rem] leading-none text-(--ui-text-tertiary)">
      <SessionRowCardSquares executionUnknown={executionUnknown} session={session} />
      {gatewayIcon}
    </span>
  )
}
