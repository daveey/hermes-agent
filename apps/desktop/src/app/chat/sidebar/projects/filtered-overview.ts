import type { ProjectInfo, SessionInfo } from '@/hermes'

import { gatewayProjectId, projectMembership, sessionIdentityKey } from './gateway-project-rows'
import { PROJECT_PREVIEW_COUNT } from './model'
import {
  excludeProjectSessions,
  NO_PROJECT_ID,
  overlayLivePreviews,
  sessionBucketId,
  type SidebarProjectTree
} from './workspace-groups'

type Placement = NonNullable<Parameters<typeof overlayLivePreviews>[4]>

export const overviewFilterPolicy = {
  limit: (filtered: boolean | undefined, all: boolean, expanded: SidebarProjectTree | null) =>
    filtered || all || expanded ? Infinity : PROJECT_PREVIEW_COUNT,
  expanded: (
    project: SidebarProjectTree,
    expanded: SidebarProjectTree | null,
    hidden?: (row: SessionInfo) => boolean
  ) => (project.filteredOverview ? null : expanded && hidden ? excludeProjectSessions(expanded, hidden) : expanded),
  rows: (
    filtered: boolean | undefined,
    expanded: SidebarProjectTree | null,
    page: SessionInfo[],
    preview: SessionInfo[]
  ) => (expanded && !filtered ? page : preview),
  offer: (
    filtered: boolean | undefined,
    all: boolean,
    expanded: SidebarProjectTree | null,
    count: number,
    hidden: number
  ) => !filtered && !all && !expanded && count > 0 && hidden > 0,
  empty: (
    force: boolean,
    grouped: boolean,
    overview: SidebarProjectTree[] | undefined,
    content: boolean,
    sessions: SessionInfo[]
  ) => force || (!grouped && !content && (overview ? overview.length === 0 : sessions.length === 0))
}

/** Narrow only the overview: an entered project's structural model stays intact.
 * Snapshot previews are not a filter result. Rebuild from loaded matching rows,
 * retaining the shared owner/lineage placement and ranking rules. */
export function filteredOverview(
  tree: SidebarProjectTree[] | undefined,
  sessions: SessionInfo[],
  projects: ProjectInfo[],
  narrow: boolean,
  isHidden: (session: SessionInfo) => boolean,
  placement: Placement,
  foreignOwner: (session: SessionInfo) => null | string
): SidebarProjectTree[] | undefined {
  if (!tree || !narrow) {
    return tree
  }

  const ownerOf = (session: SessionInfo) => foreignOwner(session) ?? ''
  const membership = new Map(tree.flatMap(node => projectMembership(node, ownerOf).map(key => [key, node.id] as const)))

  const bucketId = (session: SessionInfo) => {
    const foreign = foreignOwner(session)

    if (!foreign) {
      return sessionBucketId(session, projects, placement.owners, placement.known)
    }

    const path = (session.git_repo_root || session.cwd || '').trim()

    return (
      membership.get(sessionIdentityKey(foreign, session)) ?? (path ? gatewayProjectId(foreign, path) : NO_PROJECT_ID)
    )
  }

  const previews = overlayLivePreviews(
    tree.map(project => ({ ...project, previewSessions: [] })),
    sessions.filter(session => !isHidden(session)),
    projects,
    Infinity,
    { ...placement, bucketId }
  )

  return tree.flatMap(project => {
    const previewSessions = previews[project.id] ?? []

    return previewSessions.length ? [{ ...project, filteredOverview: true, previewSessions }] : []
  })
}
