import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { SessionInfo } from '@/hermes'
import { stampRowsWithOwningConnection } from '@/lib/session-owner-stamp'

// Cross-gateway Projects overview [daveey]. `projects.tree` answers for one
// backend; the sidebar asks every connected gateway and folds the answers into
// the active gateway's tree. Same-named projects (a repo cloned on two
// machines) share one lane; a project only one remote has gets its own lane
// under a namespaced id so it can't collide with a local id.

export interface GatewayProjectTree {
  connectionId: string
  projects: SidebarProjectTree[]
}

const REMOTE_ID_PREFIX = 'gw:'

export const remoteProjectId = (connectionId: string, id: string): string => `${REMOTE_ID_PREFIX}${connectionId}:${id}`

export const isRemoteProjectId = (id: null | string | undefined): boolean =>
  Boolean(id && id.startsWith(REMOTE_ID_PREFIX))

const sessionRecency = (session: SessionInfo): number => Number(session.last_active ?? session.started_at ?? 0) || 0

/** Merge key: Home matches Home; everything else by case-folded label. */
const projectKey = (project: SidebarProjectTree): string =>
  project.isNoProject ? '\u0000home' : `label:${project.label.trim().toLowerCase()}`

function stampTree(project: SidebarProjectTree, connectionId: string): SidebarProjectTree {
  return {
    ...project,
    ...(project.previewSessions
      ? { previewSessions: stampRowsWithOwningConnection(project.previewSessions, connectionId) }
      : {}),
    repos: (project.repos ?? []).map(repo => ({
      ...repo,
      groups: repo.groups.map(group => ({
        ...group,
        sessions: stampRowsWithOwningConnection(group.sessions, connectionId)
      }))
    }))
  }
}

function namespaceRepos(project: SidebarProjectTree, connectionId: string): SidebarProjectTree['repos'] {
  return (project.repos ?? []).map(repo => ({
    ...repo,
    id: remoteProjectId(connectionId, repo.id),
    groups: repo.groups.map(group => ({ ...group, id: remoteProjectId(connectionId, group.id) }))
  }))
}

const sum = (a: number | undefined, b: number | undefined): number | undefined =>
  a === undefined && b === undefined ? undefined : (a ?? 0) + (b ?? 0)

function mergeInto(base: SidebarProjectTree, remote: SidebarProjectTree, previewLimit: number): SidebarProjectTree {
  const seen = new Set<string>()

  const previews = [...(base.previewSessions ?? []), ...(remote.previewSessions ?? [])]
    .filter(session => (seen.has(session.id) ? false : (seen.add(session.id), true)))
    .sort((a, b) => sessionRecency(b) - sessionRecency(a))
    .slice(0, previewLimit)

  const lastActive = Math.max(base.lastActive ?? 0, remote.lastActive ?? 0)

  return {
    ...base,
    sessionCount: (base.sessionCount ?? 0) + (remote.sessionCount ?? 0),
    totalTokens: sum(base.totalTokens, remote.totalTokens),
    totalCostUsd: sum(base.totalCostUsd, remote.totalCostUsd),
    ...(lastActive ? { lastActive } : {}),
    previewSessions: previews,
    ...(base.sessionIds || remote.sessionIds
      ? { sessionIds: [...new Set([...(base.sessionIds ?? []), ...(remote.sessionIds ?? [])])] }
      : {})
  }
}

/**
 * Fold remote gateway trees into the active gateway's tree. The base keeps
 * its ids, order and structure; remote rows are stamped with their owning
 * connection so clicks resume them on the right gateway.
 */
export function mergeGatewayProjectTrees(
  base: SidebarProjectTree[],
  remotes: GatewayProjectTree[],
  previewLimit: number
): SidebarProjectTree[] {
  if (!remotes.length) {
    return base
  }

  const out = [...base]
  const indexByKey = new Map<string, number>()

  out.forEach((project, index) => {
    if (!project.archived && !indexByKey.has(projectKey(project))) {
      indexByKey.set(projectKey(project), index)
    }
  })

  for (const { connectionId, projects } of remotes) {
    for (const raw of projects) {
      if (raw.archived) {
        continue
      }

      const remote = stampTree(raw, connectionId)
      const key = projectKey(remote)
      const at = indexByKey.get(key)

      if (at !== undefined) {
        out[at] = mergeInto(out[at], remote, previewLimit)

        continue
      }

      indexByKey.set(key, out.length)
      out.push({
        ...remote,
        id: remoteProjectId(connectionId, remote.id),
        // A remote folder can't be opened, themed or used as a cwd from here.
        path: null,
        isAuto: true,
        repos: namespaceRepos(remote, connectionId)
      })
    }
  }

  return out
}

/** Remote trees contribute their session ids to the scoped (live) set too. */
export function remoteScopedSessionIds(remotes: GatewayProjectTree[]): string[] {
  return remotes.flatMap(({ projects }) => projects.flatMap(project => project.sessionIds ?? []))
}
