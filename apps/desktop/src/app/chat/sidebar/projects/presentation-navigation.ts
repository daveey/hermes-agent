import type { SessionInfo } from '@/hermes'

import { sidebarProjectPolicy } from '../project-filter'

import { withGatewayProjectRows } from './gateway-project-rows'
import { groupProjects, presentationGroupOf, type ProjectPresentationGroup } from './presentation-groups'
import type { SidebarProjectTree } from './workspace-groups'

// This catalog is a navigation lookup only. Its nodes never enter ownership, overlays or hidden counts.
function catalog(
  tree: SidebarProjectTree[],
  rows: SessionInfo[],
  foreignOwner: (row: SessionInfo) => null | string,
  windowOwner?: string
) {
  return groupProjects(withGatewayProjectRows(tree, rows, foreignOwner), windowOwner)
}

function expandFilter(ids: readonly string[], groups: ProjectPresentationGroup[]): string[] {
  return [...new Set(ids.flatMap(id => presentationGroupOf(groups, id)?.memberIds ?? []))]
}

function toggleFilter(ids: readonly string[], group: ProjectPresentationGroup): string[] {
  return group.memberIds.some(id => ids.includes(id))
    ? ids.filter(id => !group.memberIds.includes(id))
    : [...ids, ...group.memberIds]
}

function shown(
  nodes: SidebarProjectTree[],
  scope: string,
  groups: ProjectPresentationGroup[],
  dismissed: readonly string[] = []
) {
  const entered = presentationGroupOf(groups, scope)

  return nodes.filter(
    node =>
      !dismissed.includes(node.id) &&
      sidebarProjectPolicy.shown([node], entered?.memberIds.includes(node.id) ? node.id : scope).length
  )
}

function matches(row: SessionInfo, ids: readonly string[], groups: ProjectPresentationGroup[], baseMatch: boolean) {
  if (baseMatch || !ids.length) {
    return true
  }

  return groups.some(group =>
    group.members.some(
      member =>
        ids.includes(member.id) &&
        [
          ...(member.previewSessions ?? []),
          ...member.repos.flatMap(repo => repo.groups.flatMap(lane => lane.sessions))
        ].some(candidate => candidate.id === row.id && candidate.connection_id === row.connection_id)
    )
  )
}

export const presentationNavigation = { catalog, expandFilter, toggleFilter, shown, matches }
