import { useStoreSelector } from '@/lib/use-session-slice'
import { $kanbanObservationTick, hasFreshKanbanObservation } from '@/store/kanban-observation'
import { $sessionDotStateById, withRowActivity } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

/** Fold tree-only row evidence too, and repaint when its observation expires. */
export function useSessionDotState(id: null | string, row?: null | SessionInfo) {
  const state = useStoreSelector($sessionDotStateById, states => id ? (states[id] ?? 'idle') : 'draft')
  useStoreSelector($kanbanObservationTick, () => hasFreshKanbanObservation(row ?? undefined))

  return id ? withRowActivity(state, row ?? {}) : state
}
