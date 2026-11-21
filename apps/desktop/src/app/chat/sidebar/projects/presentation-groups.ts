import type { SessionInfo } from '@/hermes'

import type { SidebarProjectTree } from './workspace-groups'

// Presentation-only grouping of the final flat project overview: same-named projects from several
// machines read as one shell. Nothing here places, renames or re-ids a node.

export interface ProjectPresentationGroup {
  /** Same-name key (trimmed, lowercased); Home keeps its own key. */
  key: string
  /** The member the shell's header speaks for (metadata, "+" route). */
  primary: SidebarProjectTree
  /** Primary first, then most recently active. The nodes themselves, never copies. */
  members: SidebarProjectTree[]
  memberIds: string[]
}

const HOME_KEY = '\u0000home'
const LOCAL_STAMP = 'local'

const stampOf = (session: SessionInfo): string => (session.connection_id ?? '').trim()

const recency = (session: SessionInfo): number => Number(session.last_active ?? session.started_at ?? 0) || 0

const rowsOf = (node: SidebarProjectTree): SessionInfo[] => [
  ...(node.previewSessions ?? []),
  ...(node.repos ?? []).flatMap(repo => repo.groups.flatMap(group => group.sessions))
]

/**
 * The gateway that owns a project, or null for the window's own gateway. Read only from what the
 * store records: the node's connection, a remote project's connection, else the row stamps (every
 * row foreign => foreign). Never from ids or paths.
 */
export function projectPresentationOwner(node: SidebarProjectTree, windowOwner = LOCAL_STAMP): null | string {
  if (node.connectionId) {
    return node.connectionId === windowOwner ? null : node.connectionId
  }

  if (node.remoteProject?.connectionId) {
    return node.remoteProject.connectionId === windowOwner ? null : node.remoteProject.connectionId
  }

  const rows = rowsOf(node)

  if (!rows.length || !rows.every(row => stampOf(row) && stampOf(row) !== windowOwner)) {
    return null
  }

  return stampOf(rows.reduce((best, row) => (recency(row) > recency(best) ? row : best)))
}

/** Primary rank: window gateway's own project, then its auto-discovered one, then a foreign one. */
const rank = (node: SidebarProjectTree, windowOwner: string): number =>
  projectPresentationOwner(node, windowOwner) ? 2 : node.isAuto ? 1 : 0

const recentFirst = (a: SidebarProjectTree, b: SidebarProjectTree): number => (b.lastActive ?? 0) - (a.lastActive ?? 0)

const nameKey = (node: SidebarProjectTree): string =>
  node.isNoProject ? `${HOME_KEY}:${node.id}` : `name:${node.label.trim().toLowerCase()}`

/** Groups in the position of each group's first member in `nodes`; Home is always a singleton. */
export function groupProjects(nodes: SidebarProjectTree[], windowOwner = LOCAL_STAMP): ProjectPresentationGroup[] {
  const byKey = new Map<string, SidebarProjectTree[]>()

  for (const node of nodes) {
    const key = nameKey(node)

    byKey.set(key, [...(byKey.get(key) ?? []), node])
  }

  return [...byKey].map(([key, found]) => {
    const primary = [...found].sort(
      (a, b) => rank(a, windowOwner) - rank(b, windowOwner) || (rank(a, windowOwner) === 2 ? recentFirst(a, b) : 0)
    )[0]

    const members = [primary, ...found.filter(node => node !== primary).sort(recentFirst)]

    return { key, primary, members, memberIds: members.map(node => node.id) }
  })
}

/** The group a project id (primary or member) belongs to. */
export const presentationGroupOf = (
  groups: ProjectPresentationGroup[],
  id: string
): ProjectPresentationGroup | undefined => groups.find(group => group.memberIds.includes(id))

/** What drag-and-drop sorts: one id per group, its primary's. */
export const groupSortableIds = (groups: ProjectPresentationGroup[]): string[] => groups.map(group => group.primary.id)

/** A reordered list of group (primary) ids back to the flat member ids the existing order persists. */
export const expandGroupOrder = (groups: ProjectPresentationGroup[], orderedPrimaryIds: string[]): string[] =>
  orderedPrimaryIds.flatMap(id => groups.find(group => group.primary.id === id)?.memberIds ?? [id])
