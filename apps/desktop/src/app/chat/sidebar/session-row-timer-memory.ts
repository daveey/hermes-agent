import type { SessionInfo } from '@/hermes'
import { hasFreshKanbanObservation } from '@/store/kanban-observation'

type Cards = NonNullable<SessionInfo['kanban_timed_cards']>
type ConfirmedTimers = { cards: Cards; more: number; confirmedAt: number }
const retained = new Map<string, ConfirmedTimers>()
const MAX_AGE_MS = 15 * 60 * 1000
const MAX_ROWS = 2000
const key = (s: SessionInfo) => JSON.stringify([s.connection_id ?? null, s.profile ?? 'default', s.id])

/** Commit authoritative evidence only; display ticks and unavailable/offline rows cannot renew it. */
export function rememberRowTimers(session: SessionInfo): void {
  if (session.offline_cached || session.kanban_unknown || !session.id) {
    return
  }

  const id = key(session)

  const cards = (session.kanban_timed_cards ?? []).filter(
    c => c.state === 'running' || c.state === 'blocked' || (c.state === 'waiting' && c.wait_kind === 'deploy')
  )

  retained.delete(id)

  if (!cards.length) {
    return
  }

  const now = Date.now()
  const observed = (session.kanban_observed_at ?? now / 1000) * 1000
  const confirmedAt = Number.isFinite(observed) ? Math.min(now, observed) : now

  for (const [k, entry] of retained) {
    if (now - entry.confirmedAt >= MAX_AGE_MS) {
      retained.delete(k)
    }
  }

  if (retained.size >= MAX_ROWS) {
    retained.delete(retained.keys().next().value!)
  }

  retained.set(id, { cards, more: session.kanban_timed_more ?? 0, confirmedAt })
}

export function rowTimers(session: SessionInfo): ConfirmedTimers {
  const empty = { cards: [], more: 0, confirmedAt: 0 }

  if (session.offline_cached) {
    return empty
  }

  if (session.kanban_timed_cards?.length) {
    const uncertain = session.kanban_unknown || !hasFreshKanbanObservation(session)

    const unknownDeploy = Boolean(
      session.kanban_external_unknown &&
      session.kanban_timed_cards.some(c => c.state === 'waiting' && c.wait_kind === 'deploy')
    )

    const observed = (session.kanban_observed_at ?? 0) * 1000
    const age = Date.now() - observed

    const cards = session.kanban_timed_cards.filter(
      c =>
        !uncertain ||
        c.state !== 'waiting' ||
        (c.wait_kind === 'deploy' && age >= 0 && age < MAX_AGE_MS && observed > 0)
    )

    return {
      cards,
      more: session.kanban_timed_more ?? 0,
      confirmedAt: (uncertain || unknownDeploy) && cards.some(c => c.state === 'waiting') ? observed : 0
    }
  }

  const entry = retained.get(key(session))
  const age = entry ? Date.now() - entry.confirmedAt : -1

  return session.kanban_unknown && entry && age >= 0 && age < MAX_AGE_MS ? entry : empty
}

export function resetRowTimersForTests(): void {
  retained.clear()
}
