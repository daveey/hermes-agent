import type { SessionInfo } from '@/hermes'
import { sessionActivityAt } from '@/lib/session-branch-tree'
import { sessionHasOpenCards } from '@/store/session-filter'

export function isKanbanOwnerSession(row: SessionInfo): boolean {
  return row.id.startsWith('kanban_owner_') || row._lineage_root_id?.startsWith('kanban_owner_') === true
}

function partition(rows: SessionInfo[]) {
  return {
    pinned: rows.filter(row => !isKanbanOwnerSession(row)),
    background: rows
      .filter(row => isKanbanOwnerSession(row) && sessionHasOpenCards(row))
      .sort((a, b) => sessionActivityAt(b) - sessionActivityAt(a))
  }
}

export const ownerChatPolicy = { partition }
