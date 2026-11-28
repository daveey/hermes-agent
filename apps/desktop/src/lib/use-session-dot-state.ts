import type { ReadableAtom } from 'nanostores'

import { useStoreSelector, useStoresSelector } from '@/lib/use-session-slice'
import { $backgroundRunningSessionIds } from '@/store/composer-status'
import { rowExecutionStores } from '@/store/execution-observation-row'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $messagingSessions, $sessions, lineageAliases, sessionMatchesStoredId } from '@/store/session'
import {
  $delegatingSessionIds,
  $sessionDotStateById,
  type BackgroundDotLabel,
  backgroundDotLabel,
  type SessionDotState,
  withRowActivity
} from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

/** [daveey] Every store a dot's row verdict reads: the observation tick (time-only expiry) and `rowExecution`'s own
 *  inputs, which include the owner route and the listed `$sessions` / `$messagingSessions` rows a missing caller row
 *  falls back to. Subscribing to fewer left the dot up to one 15 s tick behind a roster, event or owner change. */
let dotRowStoreList: null | readonly ReadableAtom<unknown>[] = null

const dotRowStores = () => (dotRowStoreList ??= [$kanbanObservationTick, ...rowExecutionStores()])

const NO_ROW: Partial<SessionInfo> = {}

/** [daveey] The row a dot evaluates: the caller's, else the listed `$sessions` / `$messagingSessions` row the dot store
 *  claimed from (a contrib tab lead or an unresolved `tileStoredRow` passes none). Evaluating `{}` instead read a
 *  store-claimed process or goal as unknown work. */
const dotRow = (id: string, row: null | SessionInfo | undefined): Partial<SessionInfo> =>
  row ??
  $sessions.get().find(s => sessionMatchesStoredId(s, id)) ??
  $messagingSessions.get().find(s => sessionMatchesStoredId(s, id)) ??
  NO_ROW

/** Fold tree-only row evidence too, and repaint when its observation expires or its execution evidence changes. */
export function useSessionDotState(id: null | string, row?: null | SessionInfo) {
  const state = useStoreSelector($sessionDotStateById, states => (id ? (states[id] ?? 'idle') : 'draft'))

  return useStoresSelector(dotRowStores(), () => (id ? withRowActivity(state, dotRow(id, row)) : state))
}

/** Whether any lineage alias of `id` is in `ids` (the store claims every alias the same way). */
const litFor = (ids: readonly string[], id: null | string): boolean =>
  id !== null && ids.length > 0 && lineageAliases(id, $sessions.get()).some(alias => ids.includes(alias))

/** [daveey] What a `background` dot's label says (see `backgroundDotLabel`); `running` for every other state, where
 *  the variant ignores it. */
export function useBackgroundDotLabel(
  id: null | string,
  row: null | SessionInfo | undefined,
  state: SessionDotState
): BackgroundDotLabel {
  const background = state === 'background' && id !== null
  const processLive = useStoreSelector($backgroundRunningSessionIds, ids => background && litFor(ids, id))
  const delegating = useStoreSelector($delegatingSessionIds, ids => background && litFor(ids, id))

  return useStoresSelector(dotRowStores(), () =>
    background && id !== null ? backgroundDotLabel(dotRow(id, row), processLive || delegating) : 'running'
  )
}
