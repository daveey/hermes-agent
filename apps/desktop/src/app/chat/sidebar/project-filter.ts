/**
 * Narrow the persisted sidebar project filter to ids the ACTIVE tree resolves.
 * The filter's storage is shared across profiles and outlives project
 * deletes/rebuilds, so ids that resolve nowhere must be inert, not fatal
 * (#96246, #97762). Fail-open while the tree is still loading.
 */
export function resolveLiveProjectFilter(
  projectFilter: readonly string[],
  tree: readonly { id: string }[] | null | undefined
): readonly string[] {
  if (!projectFilter.length) {
    return projectFilter
  }

  if (!tree || !tree.length) {
    return []
  }

  const liveIds = new Set(tree.map(project => project.id))

  return projectFilter.filter(id => liveIds.has(id))
}

/**
 * The sidebar overview is a session history, not a disk browser (#53004): an
 * auto-promoted repo with no sessions yet stays out of the sidebar until work
 * lands there (it reappears the moment it owns a session). Explicit projects
 * and the Home bucket always render — the user created or kept those. Session
 * counts come from the backend `projects.tree`, so this stays live without
 * client-side re-derivation.
 */
/** Fork choice (daveey): projects list by name, and empty projects stay out of the sidebar. */
export const SIDEBAR_PROJECTS_ALPHABETICAL = true

/** Sort options for the overview: alphabetical (fork) or the `created` sidebar ordering's stable sort. */
export const sidebarProjectOrder = (ordering: string) => ({
  alphabetical: SIDEBAR_PROJECTS_ALPHABETICAL,
  stable: ordering === 'created'
})

/** Drag-reorder is meaningless under a name sort, so the handle goes away with it. */
export const projectReorderHandler = <F>(showAllProfiles: boolean, handler: F): F | undefined =>
  showAllProfiles || SIDEBAR_PROJECTS_ALPHABETICAL ? undefined : handler

/**
 * Stricter variant: drop EVERY project with no sessions, explicit ones too.
 * Home always stays, and so does `keepId` (the entered project), so drilling
 * into a just-created empty project doesn't bounce you back to the overview.
 */
export function filterToNonEmptyProjects<T extends { id: string; isNoProject?: boolean; sessionCount?: number }>(
  projects: readonly T[],
  keepId?: null | string
): T[] {
  return projects.filter(project => project.isNoProject || project.id === keepId || (project.sessionCount ?? 0) > 0)
}

/** The projects the overview lists: session-bearing, and (fork) never empty except Home/the entered one. */
export function sidebarProjectsShown<
  T extends { id: string; isAuto?: boolean; isNoProject?: boolean; sessionCount?: number }
>(projects: readonly T[], keepId?: null | string): T[] {
  const bearing = filterToSessionBearingProjects(projects)

  return SIDEBAR_PROJECTS_ALPHABETICAL ? filterToNonEmptyProjects(bearing, keepId) : bearing
}

export function filterToSessionBearingProjects<
  T extends { isAuto?: boolean; isNoProject?: boolean; sessionCount?: number }
>(projects: readonly T[]): T[] {
  return projects.filter(project => !project.isAuto || project.isNoProject || (project.sessionCount ?? 0) > 0)
}

/** The fork's sidebar project policy, bundled so the at-cap sidebar facade imports it in one line. */
export const sidebarProjectPolicy = {
  order: sidebarProjectOrder,
  reorderHandler: projectReorderHandler,
  shown: sidebarProjectsShown
}
