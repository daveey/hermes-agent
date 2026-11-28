import { useEffect, useRef, useState } from 'react'

import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { hasFreshKanbanObservation } from '@/store/kanban-observation'

import { timerColour } from './session-row-deploy'
import { rememberRowTimers, rowTimers } from './session-row-timer-memory'
import { useWaitClock } from './use-wait-clock'

type TimedCard = NonNullable<SessionInfo['kanban_timed_cards']>[number]

export function compactElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))

  if (s >= 86400) {
    return `${Math.floor(s / 86400)}d`
  }

  if (s >= 3600) {
    const minutes = Math.floor((s % 3600) / 60)

    return `${Math.floor(s / 3600)}h${minutes ? `${minutes}m` : ''}`
  }

  return s >= 60 ? `${Math.floor(s / 60)}m` : `${s}s`
}

/** At most 40% of the row (160px), preserving title, PR chip and the final gateway mark. */
export function SessionRowCardTimers({ session }: { session: SessionInfo }) {
  'use no memo' // The retained evidence expires on local display ticks, not only on row replacements.

  const { t } = useI18n()
  const ref = useRef<HTMLSpanElement>(null)
  const [budget, setBudget] = useState(160)

  useEffect(() => rememberRowTimers(session), [session])
  const timers = rowTimers(session)

  const cards = [...timers.cards]
    .filter(
      c =>
        Number.isFinite(c.since) &&
        c.since > 0 &&
        (c.state === 'running' ||
          c.state === 'blocked' ||
          (c.state === 'waiting' &&
            ((hasFreshKanbanObservation(session) && !session.kanban_unknown) ||
              (c.wait_kind === 'deploy' && timers.confirmedAt > 0)) &&
            c.since <= Date.now() / 1000))
    )
    .sort(
      (a, b) =>
        Number(a.state === 'blocked') - Number(b.state === 'blocked') || a.since - b.since || a.id.localeCompare(b.id)
    )

  const hasCards = cards.length > 0

  const now = useWaitClock(
    cards.some(c => Date.now() / 1000 - c.since < 60),
    hasCards
  )

  useEffect(() => {
    const row = ref.current?.closest('[data-sidebar-session-id]')

    if (!hasCards || !row || typeof ResizeObserver === 'undefined') {
      return
    }

    const observer = new ResizeObserver(([entry]) => setBudget(Math.min(160, entry.contentRect.width * 0.4)))

    observer.observe(row)

    return () => observer.disconnect()
  }, [hasCards])

  if (!cards.length) {
    return null
  }

  const text = (c: TimedCard) => compactElapsed(now - c.since)

  // Historical elapsed clocks remain useful, but cannot renew an answered ask.
  const unknown = Boolean(
    session.kanban_unknown ||
    timers.confirmedAt ||
    (session.kanban_observed_at !== undefined && !hasFreshKanbanObservation(session))
  )

  const label =
    (unknown ? `${t.sidebar.row.kanbanUnknown(cards.length)}\n` : '') +
    cards
      .map(c =>
        unknown
          ? `${c.title} · ${text(c)}`
          : `${c.title} · ${c.state === 'waiting' ? (c.wait_kind === 'deploy' ? t.sidebar.row.cardColours.deploy : t.sidebar.row.outcomeWaiting) : t.sidebar.row.cardColours[c.state === 'running' ? 'blue' : 'red']} · ${text(c)}`
      )
      .join('\n')

  let used = 12

  const visible = cards.slice(0, 5).filter(c => {
    used += text(c).length * 6 + 5

    return used <= budget
  })

  const more = cards.length - visible.length + timers.more

  const confirmation = timers.confirmedAt
    ? `\n${t.sidebar.row.timersLastConfirmed(Math.max(0, Math.floor((now * 1000 - timers.confirmedAt) / 60_000)))}`
    : ''

  const title = label + (timers.more ? `\n… +${timers.more}` : '') + confirmation

  return (
    <Tip label={<span className="whitespace-pre-line">{title}</span>} side="top">
      <span
        aria-label={title}
        className={`pointer-events-auto ml-1 inline-flex items-baseline gap-1 align-middle tabular-nums${timers.confirmedAt ? ' opacity-60' : ''}`}
        data-testid="session-row-card-timers"
        ref={ref}
      >
        {visible.map(c => (
          <span
            className={unknown && c.state !== 'waiting' ? 'text-muted-foreground' : timerColour(session, c, now)}
            data-card-timer={c.state}
            data-wait-kind={c.state === 'waiting' ? c.wait_kind : undefined}
            key={c.id}
          >
            {text(c)}
          </span>
        ))}
        {more > 0 && <span data-timer-overflow>…</span>}
      </span>
    </Tip>
  )
}
