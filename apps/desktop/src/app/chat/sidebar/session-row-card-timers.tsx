import { useEffect, useRef, useState } from 'react'

import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'

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
  const { t } = useI18n()
  const ref = useRef<HTMLSpanElement>(null)
  const [budget, setBudget] = useState(160)

  // Retained rows are historical, not evidence of current running work or a live ask.
  const cards = [...(session.offline_cached ? [] : (session.kanban_timed_cards ?? []))]
    .filter(c => Number.isFinite(c.since) && c.since > 0 && (c.state === 'running' || c.state === 'blocked'))
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

  const label = cards
    .map(c => `${c.title} · ${t.sidebar.row.cardColours[c.state === 'running' ? 'blue' : 'red']} · ${text(c)}`)
    .join('\n')

  let used = 12

  const visible = cards.slice(0, 5).filter(c => {
    used += text(c).length * 6 + 5

    return used <= budget
  })

  const more = cards.length - visible.length + (session.kanban_timed_more ?? 0)
  const title = label + (session.kanban_timed_more ? `\n… +${session.kanban_timed_more}` : '')

  return (
    <Tip label={<span className="whitespace-pre-line">{title}</span>} side="top">
      <span
        aria-label={title}
        className="pointer-events-auto ml-1 inline-flex items-baseline gap-1 align-middle tabular-nums"
        data-testid="session-row-card-timers"
        ref={ref}
      >
        {visible.map(c => (
          <span
            className={c.state === 'running' ? 'text-sky-400' : 'text-red-500'}
            data-card-timer={c.state}
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
