import { computed } from 'nanostores'

import { NO_PROJECT_ID } from '@/app/chat/sidebar/projects/workspace-groups'
import { $projectScope } from '@/store/project-scope'
import { $projects, resolveNewSessionCwd } from '@/store/projects'
import { $currentCwd, $newChatWorkspaceTarget, $workspaceCwdOwner, type NewChatWorkspaceTarget } from '@/store/session'
import { $focusedSessionIsTile } from '@/store/session-focus'
import { $sessionTiles } from '@/store/session-states'
import { $mainSidebarDraftCwd } from '@/store/sidebar-draft'

/** The draft preview and first send must resolve the same workspace intent. */
export function resolveMainDraftCwd(
  target: NewChatWorkspaceTarget,
  homeScope: boolean,
  currentCwd: string,
  fallbackCwd: string
): string {
  if (target === null || (target === undefined && homeScope)) {
    return ''
  }

  if (typeof target === 'string') {
    return target.trim()
  }

  return currentCwd.trim() || fallbackCwd
}

/** The only boundary lending main's ambient cwd to a different chat. */
export function captureMainDraftBeforeTile(dir: string, cwd?: null | string): void {
  if (dir === 'center' && cwd) {
    $mainSidebarDraftCwd.set($mainDraftSessionCwd.get())
  }
}

export const $mainDraftSessionCwd = computed(
  [
    $newChatWorkspaceTarget,
    $projectScope,
    $currentCwd,
    $focusedSessionIsTile,
    $mainSidebarDraftCwd,
    $projects,
    $workspaceCwdOwner,
    $sessionTiles
  ],
  (target, scope, currentCwd, tileFocused, initialCwd, _projects, cwdOwner, tiles) => {
    // A center tile lends its cwd to Files even after focus returns to main.
    const tileCwd = tileFocused || tiles.some(tile => tile.storedSessionId === cwdOwner)

    return resolveMainDraftCwd(
      target,
      scope === NO_PROJECT_ID,
      tileCwd ? initialCwd || '' : currentCwd,
      tileCwd ? initialCwd || '' : resolveNewSessionCwd()
    )
  }
)
