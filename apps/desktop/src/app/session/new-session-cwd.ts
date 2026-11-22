import { computed } from 'nanostores'

import { NO_PROJECT_ID } from '@/app/chat/sidebar/projects/workspace-groups'
import { $projectScope } from '@/store/project-scope'
import { $projects, resolveNewSessionCwd } from '@/store/projects'
import { $currentCwd, $newChatWorkspaceTarget, type NewChatWorkspaceTarget } from '@/store/session'
import { $focusedSessionIsTile } from '@/store/session-focus'
import { $mainSidebarDraftCwd, $sidebarBorrowedTileCwd } from '@/store/sidebar-draft'

/** Pure extraction of the pre-existing first-send workspace computation. */
export function resolveMainDraftCwd(
  target: NewChatWorkspaceTarget,
  homeScope: boolean,
  currentCwd: string,
  fallbackCwd: string
): string {
  return target === null || (target === undefined && homeScope)
    ? ''
    : typeof target === 'string'
      ? target.trim()
      : currentCwd.trim() || fallbackCwd
}

/** Display-only snapshot before a center tile borrows the workspace for Files. */
export function captureMainDraftBeforeTile(dir: string, cwd?: null | string): void {
  if (dir === 'center' && cwd) {
    $mainSidebarDraftCwd.set($mainSidebarDisplayCwd.get())
    $sidebarBorrowedTileCwd.set(cwd)
  }
}

export const $mainDraftSessionCwd = computed(
  [$newChatWorkspaceTarget, $projectScope, $currentCwd, $projects],
  (target, scope, currentCwd) =>
    resolveMainDraftCwd(target, scope === NO_PROJECT_ID, currentCwd, resolveNewSessionCwd())
)

// Tile paint must not move the main draft's display. A subsequent main choice
// wins immediately. This selector is NEVER an input to session.create.
export const $mainSidebarDisplayCwd = computed(
  [
    $newChatWorkspaceTarget,
    $projectScope,
    $currentCwd,
    $projects,
    $mainSidebarDraftCwd,
    $sidebarBorrowedTileCwd,
    $focusedSessionIsTile
  ],
  (target, scope, currentCwd, _projects, initialCwd, borrowedCwd, tileFocused) => {
    const borrowed = tileFocused || (borrowedCwd !== null && currentCwd === borrowedCwd)
    const displayCwd = borrowed ? initialCwd || '' : currentCwd

    return resolveMainDraftCwd(
      target,
      scope === NO_PROJECT_ID,
      displayCwd,
      borrowed ? initialCwd || '' : resolveNewSessionCwd()
    )
  }
)
