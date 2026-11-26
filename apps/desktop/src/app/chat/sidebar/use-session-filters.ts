import { useCallback, useEffect, useMemo } from 'react'

import { useContributions } from '@/contrib/react/use-contributions'
import type { SessionInfo } from '@/hermes'
import { sessionOnHiddenGateway } from '@/store/gateway-visibility'
import { normalizeProfileKey } from '@/store/profile'
import type { $pullRequestsByBranch } from '@/store/pull-requests'
import { pullRequestBucket, sessionPrKey } from '@/store/pull-requests'
import type { $sessionDotStateById } from '@/store/session-dot-state'
import { sessionStatusBucket } from '@/store/session-dot-state'
import { $sidebarSessionsPrePlugin, applySessionFilters, SESSION_FILTER_AREA } from '@/store/session-filter'
import { sessionHiddenAsAutomated } from '@/store/sidebar-automated'

import { presentationNavigation as navigation } from './projects/presentation-navigation'
import { sessionMatchesProjectFilter } from './projects/workspace-groups'
import { sidebarFiltersNarrow } from './session-filters'

type MatchesArgs = Parameters<typeof sessionMatchesProjectFilter>

export interface SessionFilterInputs {
  dotStates: ReturnType<typeof $sessionDotStateById.get>
  executionNow: number
  gatewayRegistry: Parameters<typeof sessionOnHiddenGateway>[2]
  hiddenGatewayIds: Parameters<typeof sessionOnHiddenGateway>[1]
  knownProjects: MatchesArgs[4]
  navigationGroups: Parameters<typeof navigation.matches>[2]
  pinnedSessionIds: readonly string[]
  prFilter: readonly string[]
  profileFilter: readonly string[]
  projectFilter: MatchesArgs[1]
  projectOwners: MatchesArgs[3]
  projects: MatchesArgs[2]
  pullRequests: ReturnType<typeof $pullRequestsByBranch.get>
  scopedSessions: SessionInfo[]
  showAllProfiles: boolean
  showAutomated: boolean
  statusFilter: readonly string[]
}

/** The sidebar's session filters in one place: the shared predicate (core
 *  filters + `sessions.filter` plugin contributions), the filtered list, and
 *  the pre-plugin list published for the SDK's `useSidebarSessions`. */
export function useSessionFilters(inputs: SessionFilterInputs) {
  const {
    dotStates,
    executionNow,
    gatewayRegistry,
    hiddenGatewayIds,
    knownProjects,
    navigationGroups,
    pinnedSessionIds,
    prFilter,
    profileFilter,
    projectFilter,
    projectOwners,
    projects,
    pullRequests,
    scopedSessions,
    showAllProfiles,
    showAutomated,
    statusFilter
  } = inputs

  const pluginFilters = useContributions(SESSION_FILTER_AREA)

  const matchesCoreFilters = useCallback(
    // The shared tick must invalidate memoized filters even when the dot stays `background`.
    (session: SessionInfo) => {
      // [daveey] A hidden gateway's rows leave every list and lane.
      if (sessionOnHiddenGateway(session, hiddenGatewayIds, gatewayRegistry)) {
        return false
      }

      // [daveey] Bot / test rows leave every list and lane unless shown (pinned
      // and blocked / needs-input rows stay). Same predicate ⌘] / ⌘[ use.
      if (
        sessionHiddenAsAutomated(session, showAutomated, {
          nowSeconds: executionNow,
          dotState: dotStates[session.id] ?? 'idle',
          pinnedIds: pinnedSessionIds
        })
      ) {
        return false
      }

      if (statusFilter.length && !statusFilter.includes(sessionStatusBucket(dotStates[session.id]))) {
        return false
      }

      // Narrowing to a few of the profiles on screen. Scoped to one profile the
      // list is already that profile's, so a stale selection can't blank it.
      if (showAllProfiles && profileFilter.length && !profileFilter.includes(normalizeProfileKey(session.profile))) {
        return false
      }

      if (prFilter.length) {
        const key = sessionPrKey(session)

        if (!prFilter.includes(pullRequestBucket(key ? pullRequests[key] : undefined))) {
          return false
        }
      }

      return true
    },
    [
      hiddenGatewayIds,
      gatewayRegistry,
      showAutomated,
      pinnedSessionIds,
      statusFilter,
      profileFilter,
      showAllProfiles,
      prFilter,
      pullRequests,
      dotStates,
      executionNow
    ]
  )

  // Keep the SDK snapshot independent of plugin registration. A consumer may
  // re-register its contribution whenever that snapshot changes.
  const matchesNavigation = useCallback(
    (session: SessionInfo) =>
      navigation.matches(
        session,
        projectFilter,
        navigationGroups,
        sessionMatchesProjectFilter(session, projectFilter, projects, projectOwners, knownProjects)
      ),
    [projectFilter, navigationGroups, projects, projectOwners, knownProjects]
  )

  const sessionMatchesFilters = useCallback(
    (session: SessionInfo) =>
      matchesCoreFilters(session) && applySessionFilters(session, pluginFilters) && matchesNavigation(session),
    [matchesCoreFilters, pluginFilters, matchesNavigation]
  )

  const filtersNarrow = sidebarFiltersNarrow({
    hiddenGatewayIds,
    showAutomated,
    statusFilter,
    projectFilter,
    prFilter,
    showAllProfiles,
    profileFilter,
    pluginFilters
  })

  const coreFiltersNarrow = sidebarFiltersNarrow({
    hiddenGatewayIds,
    showAutomated,
    statusFilter,
    projectFilter,
    prFilter,
    showAllProfiles,
    profileFilter,
    pluginFilters: []
  })

  // What the SDK's `useSidebarSessions` hands plugins: the list with core
  // filters applied but not `sessions.filter` contributions.
  const prePluginSessions = useMemo(
    () =>
      coreFiltersNarrow
        ? scopedSessions.filter(session => matchesCoreFilters(session) && matchesNavigation(session))
        : scopedSessions,
    [scopedSessions, coreFiltersNarrow, matchesCoreFilters, matchesNavigation]
  )

  const visibleSessions = useMemo(
    () => (pluginFilters.length ? scopedSessions.filter(sessionMatchesFilters) : prePluginSessions),
    [scopedSessions, pluginFilters, sessionMatchesFilters, prePluginSessions]
  )

  useEffect(() => {
    $sidebarSessionsPrePlugin.set(prePluginSessions)
  }, [prePluginSessions])

  useEffect(() => () => $sidebarSessionsPrePlugin.set([]), [])

  // Default automated hiding is not an explicit overview filter (Reset restores it).
  const overviewNarrow = sidebarFiltersNarrow({ ...inputs, showAutomated: true, pluginFilters })

  return { filtersNarrow, overviewNarrow, pluginFilters, sessionMatchesFilters, visibleSessions }
}
