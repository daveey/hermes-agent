import { useMemo } from 'react'

import { type SessionInfo } from '@/hermes'

import { overlayLivePreviews, PROJECT_PREVIEW_COUNT, type SidebarProjectTree } from './projects'

type OverviewPreviewOptions = NonNullable<Parameters<typeof overlayLivePreviews>[4]>

// Preview rows come from the backend tree (each project carries its most-recent sessions), overlaid
// with live $sessions so a just-created session shows under its project instantly (and with its
// working arc), matching the flat Recents list. Keyed by project id for the rows. `rank` applies
// before the trim, so "3 priciest in this project" isn't "3 most recent, priciest first".
// Its own module so ChatSidebar stays under its size/complexity ratchet.
export function useOverviewPreviews(
  projectOverview: SidebarProjectTree[] | undefined,
  agentSessions: SessionInfo[],
  projects: Parameters<typeof overlayLivePreviews>[2],
  showAllSessions: boolean,
  { ownerKey, removed, owners, known, rank }: OverviewPreviewOptions
): Record<string, SessionInfo[]> {
  return useMemo(
    () =>
      overlayLivePreviews(
        projectOverview ?? [],
        agentSessions,
        projects,
        showAllSessions ? Infinity : PROJECT_PREVIEW_COUNT,
        { ownerKey, removed, owners, known, rank }
      ),
    [projectOverview, agentSessions, projects, showAllSessions, ownerKey, removed, owners, known, rank]
  )
}
