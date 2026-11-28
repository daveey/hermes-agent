import { atom, onMount } from 'nanostores'

import type { SessionInfo } from '@/types/hermes'

// Expire cached zero-work projections even when the owner stops answering.
// Mount-scoped so unused stores do not leave a permanent background timer.
export const $kanbanObservationTick = atom(0)
onMount($kanbanObservationTick, () => {
  const timer = setInterval(() => $kanbanObservationTick.set($kanbanObservationTick.get() + 1), 15_000)

  return () => clearInterval(timer)
})

/** A successful empty read is evidence; an absent/expired stamp is not. */
export function hasFreshKanbanObservation(
  session: Pick<SessionInfo, 'kanban_observed_at'> | undefined,
  after?: number
): boolean {
  const observed = session?.kanban_observed_at
  const age = typeof observed === 'number' ? Date.now() / 1000 - observed : NaN

  return (
    Number.isFinite(age) &&
    age >= 0 &&
    age <= 180 &&
    (after === undefined || (typeof observed === 'number' && observed >= after))
  )
}
