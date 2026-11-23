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

// A remote tree is never the active gateway's, so every row in it names its
// owner — `local` included: when This device is not the active gateway a bare
// row would resume on the active remote, which never minted it. [daveey]
const stampOwner = (sessions: SessionInfo[], connectionId: string): SessionInfo[] =>
  connectionId === 'local'
    ? sessions.map(session => (session.connection_id?.trim() ? session : { ...session, connection_id: 'local' }))
    : stampRowsWithOwningConnection(sessions, connectionId)

function stampTree(project: SidebarProjectTree, connectionId: string): SidebarProjectTree {
  return {
    ...project,
    ...(project.sessionMembership
      ? {
          sessionMembership: project.sessionMembership.map(ref => ({
            ...ref,
            connection_id: ref.connection_id || connectionId
          }))
        }
      : {}),
    ...(project.previewSessions ? { previewSessions: stampOwner(project.previewSessions, connectionId) } : {}),
    repos: (project.repos ?? []).map(repo => ({
      ...repo,
      groups: repo.groups.map(group => ({
        ...group,
        sessions: stampOwner(group.sessions, connectionId)
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
    sessionMembership: [...(base.sessionMembership ?? []), ...(remote.sessionMembership ?? [])],
    ...(base.sessionIds || remote.sessionIds
      ? { sessionIds: [...new Set([...(base.sessionIds ?? []), ...(remote.sessionIds ?? [])])] }
      : {})
  }
}

/** Every session id a project subtree claims (ids, previews, lane rows). */
function claimedIds(project: SidebarProjectTree): string[] {
  return [
    ...(project.sessionIds ?? []),
    ...(project.previewSessions ?? []).map(session => session.id),
    ...(project.repos ?? []).flatMap(repo => repo.groups.flatMap(group => group.sessions.map(session => session.id)))
  ]
}

/**
 * Strip rows an earlier tree (the active gateway first) already placed. A
 * gateway can report another host's session — nous-1 lists MacBook chats, and
 * with no `/Users/...` folder there they land in its Home — so without this the
 * same chat renders twice: the local, live row (kanban badge, stripe) plus a
 * frozen remote copy in Home. Returns null when nothing of the project is left.
 * [daveey]
 */
function withoutClaimed(project: SidebarProjectTree, claimed: ReadonlySet<string>): null | SidebarProjectTree {
  const ids = claimedIds(project)

  if (!ids.some(id => claimed.has(id))) {
    return project
  }

  const keep = (session: SessionInfo): boolean => !claimed.has(session.id)
  const sessionIds = project.sessionIds?.filter(id => !claimed.has(id))
  const previewSessions = project.previewSessions?.filter(keep)

  const repos = (project.repos ?? []).map(repo => {
    const groups = repo.groups.map(group => ({ ...group, sessions: group.sessions.filter(keep) }))

    return { ...repo, groups, sessionCount: groups.reduce((n, group) => n + group.sessions.length, 0) }
  })

  const dropped = project.sessionIds
    ? project.sessionIds.length - (sessionIds?.length ?? 0)
    : (project.previewSessions?.length ?? 0) - (previewSessions?.length ?? 0)

  const sessionCount = Math.max(0, (project.sessionCount ?? 0) - dropped)

  if (sessionCount === 0 && !previewSessions?.length) {
    return null
  }

  return {
    ...project,
    sessionCount,
    repos,
    ...(previewSessions ? { previewSessions } : {}),
    ...(sessionIds ? { sessionIds } : {}),
    ...(project.sessionMembership
      ? { sessionMembership: project.sessionMembership.filter(ref => !claimed.has(ref.id)) }
      : {})
  }
}

/**
 * Fold remote gateway trees into the active gateway's tree. The base keeps
 * its ids, order and structure; remote rows are stamped with their owning
 * connection so clicks resume them on the right gateway. A session already
 * placed by the base (or an earlier remote) is not repeated.
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

  const claimed = new Set(base.flatMap(claimedIds))

  for (const { connectionId, projects } of remotes) {
    const fresh: SidebarProjectTree[] = []

    for (const raw of projects) {
      const unique = raw.archived ? null : withoutClaimed(raw, claimed)

      if (unique) {
        fresh.push(unique)
      }
    }

    // Claim after the whole gateway: its own rows never shadow each other.
    for (const project of fresh) {
      for (const id of claimedIds(project)) {
        claimed.add(id)
      }
    }

    for (const unique of fresh) {
      const remote = stampTree(unique, connectionId)
      const key = projectKey(remote)
      const at = indexByKey.get(key)

      if (at !== undefined) {
        out[at] = mergeInto(out[at], remote, previewLimit)

        continue
      }

      indexByKey.set(key, out.length)
      out.push({
        ...remote,
        ...(!remote.isAuto && !remote.isNoProject && remote.id.startsWith('p_')
          ? { remoteProject: { connectionId, id: remote.id } }
          : {}),
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
