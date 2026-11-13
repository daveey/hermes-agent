import { useStoreSelector } from '@/lib/use-session-slice'
import { $backgroundRunningSessionIds } from '@/store/composer-status'
import { $kanbanObservationTick, hasFreshKanbanObservation } from '@/store/kanban-observation'
import { $sessions, lineageAliases } from '@/store/session'
import {
  $delegatingSessionIds,
  $sessionDotStateById,
  backgroundDotUnknown,
  type SessionDotState,
  withRowActivity
} from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

/** Fold tree-only row evidence too, and repaint when its observation expires. */
export function useSessionDotState(id: null | string, row?: null | SessionInfo) {
  const state = useStoreSelector($sessionDotStateById, states => id ? (states[id] ?? 'idle') : 'draft')
  useStoreSelector($kanbanObservationTick, () => hasFreshKanbanObservation(row ?? undefined))

  return id ? withRowActivity(state, row ?? {}) : state
}

/** Whether any lineage alias of `id` is in `ids` (the store claims every alias the same way). */
const litFor = (ids: readonly string[], id: null | string): boolean =>
  id !== null && ids.length > 0 && lineageAliases(id, $sessions.get()).some(alias => ids.includes(alias))

/** [daveey] Whether a `background` dot's label must say its status is unknown (see `backgroundDotUnknown`). Reads
 *  only stores the dot state already derives from; the existing observation tick re-evaluates time-only expiry. */
export function useBackgroundDotUnknown(
  id: null | string,
  row: null | SessionInfo | undefined,
  state: SessionDotState
): boolean {
  const background = state === 'background' && id !== null
  const processLive = useStoreSelector($backgroundRunningSessionIds, ids => background && litFor(ids, id))
  const delegating = useStoreSelector($delegatingSessionIds, ids => background && litFor(ids, id))

  return useStoreSelector($kanbanObservationTick, () =>
    background ? backgroundDotUnknown(row ?? {}, processLive || delegating) : false
  )
}
