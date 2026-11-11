// Pure helper for applyRenamedSessionTitle (store/projects.ts), split out to keep that file under its line ratchet.

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

/** Patch every cached copy of the renamed conversation inside one project
 *  node (lane rows + overview previews), keeping the node's reference when
 *  nothing matched — the tree keeps unchanged nodes by reference so the
 *  drill-in doesn't refetch on a no-op (#77591). */
export function renameProjectSessions(
  project: SidebarProjectTree,
  sessionId: string,
  next: string | null
): SidebarProjectTree {
  let changed = false

  const rename = (sessions: SessionInfo[]): SessionInfo[] =>
    sessions.map(session => {
      if (!sessionMatchesStoredId(session, sessionId) || session.title === next) {
        return session
      }

      changed = true

      return { ...session, title: next }
    })

  const repos = project.repos.map(repo => {
    let repoChanged = false

    const groups = repo.groups.map(group => {
      const sessions = rename(group.sessions)

      if (sessions === group.sessions) {
        return group
      }

      repoChanged = true

      return { ...group, sessions }
    })

    if (!repoChanged) {
      return repo
    }

    return { ...repo, groups, sessionCount: groups.reduce((n, g) => n + g.sessions.length, 0) }
  })

  const previewSessions = project.previewSessions ? rename(project.previewSessions) : project.previewSessions

  if (!changed) {
    return project
  }

  return { ...project, previewSessions, repos, sessionCount: repos.reduce((n, repo) => n + repo.sessionCount, 0) }
}
