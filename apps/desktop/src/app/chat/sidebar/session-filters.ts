// Whether any sidebar filter narrows the session list, moved out of
// ChatSidebar (./index.tsx) unchanged. [daveey]
export interface SidebarFilterNarrowInput {
  hiddenGatewayIds: readonly unknown[]
  showAutomated: boolean
  statusFilter: readonly unknown[]
  projectFilter: readonly unknown[]
  prFilter: readonly unknown[]
  showAllProfiles: boolean
  profileFilter: readonly unknown[]
  /** `sessions.filter` plugin contributions: any narrows. */
  pluginFilters: readonly unknown[]
}

export function sidebarFiltersNarrow({
  hiddenGatewayIds,
  showAutomated,
  statusFilter,
  projectFilter,
  prFilter,
  showAllProfiles,
  profileFilter,
  pluginFilters
}: SidebarFilterNarrowInput): boolean {
  return (
    hiddenGatewayIds.length > 0 ||
    !showAutomated ||
    statusFilter.length > 0 ||
    projectFilter.length > 0 ||
    prFilter.length > 0 ||
    (showAllProfiles && profileFilter.length > 0) ||
    pluginFilters.length > 0
  )
}
