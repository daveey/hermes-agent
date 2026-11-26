import { useCallback, useEffect, useMemo } from 'react'

import { type SessionInfo } from '@/hermes'
import { markSidebarFirstRows } from '@/store/boot-snapshot'
import { enterProject } from '@/store/projects'

import { overlayLivePreviews, PROJECT_PREVIEW_COUNT, type SidebarProjectTree } from './projects'
import { filteredOverview } from './projects/filtered-overview'

export function useMarkSidebarRows(drawnRows: number, sessionsLoading: boolean) {
  useEffect(() => {
    if (drawnRows > 0) {
      markSidebarFirstRows(drawnRows, !sessionsLoading)
    }
  }, [drawnRows, sessionsLoading])
}

export function useEnterOverviewProject(
  projectModel: SidebarProjectTree[],
  syncCwd: (project: SidebarProjectTree) => void
) {
  return useCallback(
    (id: string) => {
      const project = projectModel.find(node => node.id === id)

      if (project) {
        syncCwd(project)
      }

      enterProject(id)
    },
    [projectModel, syncCwd]
  )
}

type OverviewPreviewOptions = NonNullable<Parameters<typeof overlayLivePreviews>[4]> & {
  narrow: boolean
  showAllSessions: boolean
  isHidden: (session: SessionInfo) => boolean
  ownerPolicy: {
    foreignOwner: (session: SessionInfo) => null | string
    ownerKey: NonNullable<Parameters<typeof overlayLivePreviews>[4]>['ownerKey']
  }
}

// Snapshot + live previews normally stay capped; explicit filter results are
// loaded matches only, uncapped, with empty projects removed by a pure helper.
export function useOverviewPreviews(
  candidates: SidebarProjectTree[] | undefined,
  agentSessions: SessionInfo[],
  projects: Parameters<typeof overlayLivePreviews>[2],
  { showAllSessions, narrow, isHidden, ownerPolicy, removed, owners, known, rank }: OverviewPreviewOptions
) {
  const { foreignOwner, ownerKey } = ownerPolicy

  return useMemo(() => {
    const placement = { ownerKey, removed, owners, known, rank }

    const projectOverview = filteredOverview(
      candidates,
      agentSessions,
      projects,
      narrow,
      isHidden,
      placement,
      foreignOwner
    )

    const overviewPreviews = narrow
      ? Object.fromEntries((projectOverview ?? []).map(project => [project.id, project.previewSessions ?? []]))
      : overlayLivePreviews(
          projectOverview ?? [],
          agentSessions,
          projects,
          showAllSessions ? Infinity : PROJECT_PREVIEW_COUNT,
          placement
        )

    return [projectOverview, overviewPreviews] as const
  }, [
    candidates,
    agentSessions,
    projects,
    showAllSessions,
    narrow,
    isHidden,
    foreignOwner,
    ownerKey,
    removed,
    owners,
    known,
    rank
  ])
}
