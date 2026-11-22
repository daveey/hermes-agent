import { useStore } from '@nanostores/react'

import { NO_PROJECT_ID } from '@/app/chat/sidebar/projects/workspace-groups'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import { $projectScope } from '@/store/project-scope'
import { $projectTree, resolveNewSessionCwd } from '@/store/projects'
import { $configuredDefaultProjectDir, $connection, $currentCwd, $newChatWorkspaceTarget } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import { $mainSidebarDraftCwd, $sidebarBorrowedTileCwd, $sidebarBorrowedTileId } from '@/store/sidebar-draft'

/** Fresh display read only. The send path deliberately keeps its original inline expression. */
export function mainSidebarDisplayCwd(): string {
  const target = $newChatWorkspaceTarget.get()
  const scope = $projectScope.get()
  const currentCwd = $currentCwd.get()
  const borrowedCwd = $sidebarBorrowedTileCwd.get()
  const borrowedId = $sidebarBorrowedTileId.get()
  const tileOpen = $sessionTiles.get().some(tile => tile.storedSessionId === borrowedId)
  // Focus alone is not borrowing: right splits and cwd-less center tiles never capture it.
  const borrowed = tileOpen && borrowedCwd !== null && currentCwd === borrowedCwd
  const displayCwd = borrowed ? $mainSidebarDraftCwd.get() || '' : currentCwd

  return target === null || (target === undefined && scope === NO_PROJECT_ID)
    ? ''
    : typeof target === 'string'
      ? target.trim()
      : displayCwd.trim() || (borrowed ? $mainSidebarDraftCwd.get() || '' : resolveNewSessionCwd())
}

/** Subscribe to every fallback input, but evaluate afresh on every render (no computed cache). */
export function useMainSidebarDisplayCwd(): string {
  'use no memo'
  // The fallback reads stores imperatively; React Compiler must not hoist this read.
  useStore($newChatWorkspaceTarget)
  useStore($projectScope)
  useStore($currentCwd)
  useStore($projectTree)
  useStore($newChatProfile)
  useStore($activeGatewayProfile)
  useStore($configuredDefaultProjectDir)
  useStore($connection)
  useStore($mainSidebarDraftCwd)
  useStore($sidebarBorrowedTileCwd)
  useStore($sidebarBorrowedTileId)
  useStore($sessionTiles)

  return mainSidebarDisplayCwd()
}

/** Capture before a center tile borrows the workspace for Files. */
export function captureMainDraftBeforeTile(dir: string, cwd: null | string | undefined, storedId: string): void {
  if (dir === 'center' && cwd) {
    $mainSidebarDraftCwd.set(mainSidebarDisplayCwd())
    $sidebarBorrowedTileCwd.set(cwd)
    $sidebarBorrowedTileId.set(storedId)
  }
}
