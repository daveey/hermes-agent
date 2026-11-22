import { useStore } from '@nanostores/react'

import { NO_PROJECT_ID } from '@/app/chat/sidebar/projects/workspace-groups'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import { $projectScope } from '@/store/project-scope'
import { $projectTree, resolveNewSessionCwd } from '@/store/projects'
import { $configuredDefaultProjectDir, $connection, $currentCwd, $newChatWorkspaceTarget } from '@/store/session'

/** Mirror the live pre-PR send expression; never snapshot a different display target. */
export function mainSidebarDisplayCwd(): string {
  const workspaceTarget = $newChatWorkspaceTarget.get()
  const homeScope = $projectScope.get() === NO_PROJECT_ID

  return workspaceTarget === null || (workspaceTarget === undefined && homeScope)
    ? ''
    : typeof workspaceTarget === 'string'
      ? workspaceTarget.trim()
      : $currentCwd.get().trim() || resolveNewSessionCwd()
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

  return mainSidebarDisplayCwd()
}
