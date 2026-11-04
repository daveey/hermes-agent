import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { withKanbanEvidence } from '@/app/session/hooks/refresh-retained-kanban'
import type { SessionInfo } from '@/hermes'
import { normalizeProfileKey } from '@/store/profile'

// [daveey] The last task evidence (kanban_* annotations) each gateway's rows carried, by (connection, profile,
// session). Hub replica records have no task annotations, so a gateway whose rows fall back to the replica (cold
// start before its first live tree, or a failed background refresh) would paint every row with an empty kanban
// badge and no waiting/stuck stripe. Its rows keep the last counts it reported instead; their original
// `kanban_observed_at` stays, so freshness checks still treat the evidence as expired until a live answer lands.
const knownKanbanEvidence = new Map<string, SessionInfo>()

const kanbanEvidenceKey = (connectionId: string, row: SessionInfo) =>
  `${connectionId}\u0000${normalizeProfileKey(row.profile || 'default')}\u0000${row.id}`

const hasKanbanEvidence = (row: SessionInfo) =>
  typeof row.kanban_observed_at === 'number' && Number.isFinite(row.kanban_observed_at)

function treeRows(node: SidebarProjectTree): SessionInfo[] {
  return [
    ...(node.previewSessions ?? []),
    ...(node.repos ?? []).flatMap(repo => (repo.groups ?? []).flatMap(group => group.sessions ?? []))
  ]
}

export function rememberKanbanEvidence(
  nodes: readonly SidebarProjectTree[],
  connectionOf: (row: SessionInfo) => string | undefined
): void {
  for (const row of nodes.flatMap(treeRows)) {
    const connectionId = row?.id && hasKanbanEvidence(row) ? connectionOf(row) : undefined

    if (!connectionId) {
      continue
    }

    const key = kanbanEvidenceKey(connectionId, row)
    const known = knownKanbanEvidence.get(key)

    if (!known || (known.kanban_observed_at ?? 0) <= (row.kanban_observed_at ?? 0)) {
      knownKanbanEvidence.set(key, row)
    }
  }
}

function withKnownKanban(rows: SessionInfo[], connectionId: string): SessionInfo[] {
  return rows.map(row => {
    const known = hasKanbanEvidence(row) ? undefined : knownKanbanEvidence.get(kanbanEvidenceKey(connectionId, row))

    return known ? withKanbanEvidence(row, known) : row
  })
}

export function carryKanbanEvidence(node: SidebarProjectTree, connectionId: string): SidebarProjectTree {
  return {
    ...node,
    ...(node.previewSessions ? { previewSessions: withKnownKanban(node.previewSessions, connectionId) } : {}),
    ...(node.repos
      ? {
          repos: node.repos.map(repo => ({
            ...repo,
            groups: (repo.groups ?? []).map(group => ({
              ...group,
              sessions: withKnownKanban(group.sessions ?? [], connectionId)
            }))
          }))
        }
      : {})
  }
}
