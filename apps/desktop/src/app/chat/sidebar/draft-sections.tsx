import { SidebarContributedSections } from './contributed-sections'
import { SidebarDraftRows } from './draft-rows'

/** Drafts sit directly below Pinned, ahead of contributed sections and Recents. */
export function SidebarDraftSections({
  projectFilter,
  enteredProjectId,
  showArchived
}: {
  projectFilter: readonly string[]
  enteredProjectId?: string
  showArchived: boolean
}) {
  return (
    <>
      <SidebarDraftRows archived={showArchived} filter={projectFilter} projectId={enteredProjectId} />
      <SidebarContributedSections />
    </>
  )
}
