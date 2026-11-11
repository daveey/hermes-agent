// [daveey] Instant "Move to project": the sidebar files a row by the project
// tree's owner map (`$projectOwnerBySessionId`, built from `$projectTree`), and
// that map used to change only after the move RPC AND a full tree refetch
// (2-5 s on a busy all-profiles gateway). The move now reassigns the row in
// the cached tree first; every tree payload that lands while the move is
// pending re-applies it, so an older in-flight refresh can't snap the row back.

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { $sessions, sessionMatchesStoredId, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { $projectTree } from './project-tree'

type Matcher = (session: SessionInfo, storedSessionId: string) => boolean

interface PendingMove {
  at: number
  projectId: string
  row: SessionInfo | null
}

// Long enough for a slow move RPC plus its refetch; afterwards the backend is the
// only source again even if it disagreed (the move failed server-side).
const PENDING_MOVE_TTL_MS = 30_000
const pendingMoves = new Map<string, PendingMove>()

const sessionIdsOf = (row: SessionInfo | null, sessionId: string): Set<string> =>
  new Set([sessionId, ...(row ? [row.id, row._lineage_root_id ?? '', ...(row._lineage_ids ?? [])] : [])].filter(Boolean))

/**
 * `tree` with session `sessionId` owned by `targetId` only: dropped from every
 * other node's ids, previews and lanes, and added to the target's ids and
 * preview (as `row`, when given). Returns `tree` itself when it already agrees
 * or the target is not in it.
 */
export function reassignTreeSession(
  tree: SidebarProjectTree[],
  sessionId: string,
  targetId: string,
  row: SessionInfo | null,
  matches: Matcher
): SidebarProjectTree[] {
  if (!tree.some(node => node.id === targetId)) {
    return tree
  }

  const ids = sessionIdsOf(row, sessionId)
  const isMoved = (s: SessionInfo) => ids.has(s.id) || matches(s, sessionId)
  let changed = false

  const next = tree.map(node => {
    if (node.id === targetId) {
      const owned = node.sessionIds?.includes(sessionId) || node.previewSessions?.some(isMoved)

      if (owned && !row) {
        return node
      }

      changed = true
      const previews = (node.previewSessions ?? []).filter(s => !isMoved(s))

      return {
        ...node,
        previewSessions: row ? [row, ...previews] : previews,
        sessionCount: owned ? node.sessionCount : node.sessionCount + 1,
        sessionIds: [sessionId, ...(node.sessionIds ?? []).filter(id => !ids.has(id))]
      }
    }

    const heldIds = node.sessionIds?.some(id => ids.has(id))
    const heldRows = node.previewSessions?.some(isMoved)

    const heldLanes = node.repos.some(repo => repo.groups.some(group => group.sessions.some(isMoved)))

    if (!heldIds && !heldRows && !heldLanes) {
      return node
    }

    changed = true

    const repos = node.repos.map(repo => {
      const groups = repo.groups.map(group => ({ ...group, sessions: group.sessions.filter(s => !isMoved(s)) }))

      return { ...repo, groups, sessionCount: groups.reduce((n, group) => n + group.sessions.length, 0) }
    })

    return {
      ...node,
      previewSessions: node.previewSessions?.filter(s => !isMoved(s)),
      repos,
      sessionCount: Math.max(0, node.sessionCount - 1),
      sessionIds: node.sessionIds?.filter(id => !ids.has(id))
    }
  })

  return changed ? next : tree
}

/** Start a move: remember it and return the tree with the row already in `projectId`. */
export function beginPendingMove(
  tree: SidebarProjectTree[],
  sessionId: string,
  projectId: string,
  row: SessionInfo | null,
  matches: Matcher
): SidebarProjectTree[] {
  pendingMoves.set(sessionId, { at: Date.now(), projectId, row })

  return reassignTreeSession(tree, sessionId, projectId, row, matches)
}

/** A move that failed: forget it so the next tree payload restores the backend's placement. */
export function cancelPendingMove(sessionId: string): void {
  pendingMoves.delete(sessionId)
}

/** The backend tree already files the session under the target, and nowhere else. */
function backendAgrees(tree: SidebarProjectTree[], sessionId: string, projectId: string): boolean {
  return tree.every(node =>
    node.id === projectId ? Boolean(node.sessionIds?.includes(sessionId)) : !node.sessionIds?.includes(sessionId)
  )
}

/**
 * Re-apply moves still pending to a freshly built tree. A move ends once a
 * payload agrees with it (the backend has the row there now) or after the TTL.
 */
export function applyPendingMoves(tree: SidebarProjectTree[], matches: Matcher): SidebarProjectTree[] {
  const now = Date.now()
  let out = tree

  for (const [sessionId, move] of pendingMoves) {
    if (now - move.at > PENDING_MOVE_TTL_MS || backendAgrees(tree, sessionId, move.projectId)) {
      pendingMoves.delete(sessionId)

      continue
    }

    out = reassignTreeSession(out, sessionId, move.projectId, move.row, matches)
  }

  return out
}

/** Every session row the cached tree carries (previews and lanes). */
export function treeSessionRows(): SessionInfo[] {
  return $projectTree
    .get()
    .flatMap(project => [
      ...(project.previewSessions ?? []),
      ...(project.repos ?? []).flatMap(repo => repo.groups.flatMap(group => group.sessions))
    ])
}

/**
 * Paint a move before any RPC: the row's owner in the cached tree, and its cwd
 * in `$sessions` (so the destination's lanes place it). Returns the undo for a
 * failed move; the caller then refetches the tree to restore the backend's view.
 */
export function showMovedAtOnce(sessionId: string, projectId: string, cwd: string): () => void {
  const before = $sessions.get().find(s => sessionMatchesStoredId(s, sessionId))
  const row = before ?? treeSessionRows().find(s => sessionMatchesStoredId(s, sessionId)) ?? null
  const moved = row && cwd ? { ...row, cwd, git_branch: null, git_repo_root: null } : row
  $projectTree.set(beginPendingMove($projectTree.get(), sessionId, projectId, moved, sessionMatchesStoredId))

  const setCwd = (value: null | string | undefined) =>
    setSessions(prev => prev.map(s => (sessionMatchesStoredId(s, sessionId) ? { ...s, cwd: value } : s)))

  if (before && moved?.cwd) {
    setCwd(moved.cwd)
  }

  return () => {
    cancelPendingMove(sessionId)

    if (before) {
      setCwd(before.cwd)
    }
  }
}

// "Move to project" relocates the session's cwd, and the backend echoes that
// as a session.info cwd change on the selected chat — which would otherwise
// look like an agent-driven move and yank the sidebar into the destination.
// A move is filing, not navigation: remember the destination briefly so the
// follow for that cwd is skipped (local fix: sidebar-move-no-follow).
const MOVE_FOLLOW_SUPPRESS_MS = 15_000
const moveFollowSuppressed = new Map<string, number>()

export function suppressMoveFollow(cwd: string): void {
  const target = cwd.trim()

  if (target) {
    moveFollowSuppressed.set(target, Date.now() + MOVE_FOLLOW_SUPPRESS_MS)
  }
}

export function consumeMoveFollowSuppression(cwd: string): boolean {
  const until = moveFollowSuppressed.get(cwd)

  if (until === undefined) {
    return false
  }

  moveFollowSuppressed.delete(cwd)

  return until >= Date.now()
}
