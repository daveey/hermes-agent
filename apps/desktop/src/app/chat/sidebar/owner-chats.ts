import type { SessionInfo } from '@/hermes'
import { sessionActivityAt } from '@/lib/session-branch-tree'
import { hasFreshKanbanObservation } from '@/store/kanban-observation'
import { sessionHasOpenCards } from '@/store/session-filter'

export function isKanbanOwnerSession(row: SessionInfo): boolean {
  return row.id.startsWith('kanban_owner_') || row._lineage_root_id?.startsWith('kanban_owner_') === true
}

function partition(rows: SessionInfo[]) {
  return {
    pinned: rows.filter(row => !isKanbanOwnerSession(row)),
    background: rows
      .filter(row => isKanbanOwnerSession(row) && hasFreshKanbanObservation(row) && sessionHasOpenCards(row))
      .sort((a, b) => sessionActivityAt(b) - sessionActivityAt(a))
  }
}

/** Pins are window-scoped; owner-chat placement is not. Match both lineage identities
 * within the same gateway/profile, never a foreign gateway's equal session id. */
function additionalOwners(rows: SessionInfo[], pins: SessionInfo[], ownerKey: (row: SessionInfo) => string) {
  const identities = (row: SessionInfo) => [row.id, row._lineage_root_id].filter(Boolean)
  const pinned = new Set(pins.flatMap(row => identities(row).map(id => JSON.stringify([ownerKey(row), id]))))

  return rows.filter(
    row => isKanbanOwnerSession(row) && !identities(row).some(id => pinned.has(JSON.stringify([ownerKey(row), id])))
  )
}

function outsideOrdinaryLists(row: SessionInfo, isPinned: (row: SessionInfo) => boolean) {
  return isKanbanOwnerSession(row) || isPinned(row)
}

function hiddenFromProjects(
  row: SessionInfo,
  isPinned: (row: SessionInfo) => boolean,
  filtersNarrow: boolean,
  matchesFilters: (row: SessionInfo) => boolean
) {
  return outsideOrdinaryLists(row, isPinned) || (filtersNarrow && !matchesFilters(row))
}

export const ownerChatPolicy = { partition, additionalOwners, outsideOrdinaryLists, hiddenFromProjects }
