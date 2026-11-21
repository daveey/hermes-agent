import type { useSensors } from '@dnd-kit/core'
import { useStore } from '@nanostores/react'
import type * as React from 'react'

import type { SessionInfo } from '@/hermes'
import { $activeConnectionId } from '@/store/connections'

import { ReorderableList, useSortableBindings } from '../reorderable-list'

import { ProjectOverviewRow } from './overview-row'
import { ProjectGroupRow } from './presentation-group-row'
import { expandGroupOrder, groupProjects, groupSortableIds } from './presentation-groups'
import type { SidebarProjectTree } from './workspace-groups'

interface Props {
  projects: SidebarProjectTree[]
  hidden?: { isHidden: (session: SessionInfo) => boolean; counts: Record<string, number> }
  previews?: Record<string, SessionInfo[]>
  activeProjectId?: null | string
  onEnter?: (id: string) => void
  onNewSession?: React.ComponentProps<typeof ProjectOverviewRow>['onNewSession']
  onNewSessionSplit?: React.ComponentProps<typeof ProjectOverviewRow>['onNewSessionSplit']
  onReorder?: (ids: string[]) => void
  sensors?: ReturnType<typeof useSensors>
  showAllSessions: boolean
  renderRows: (rows: SessionInfo[]) => React.ReactNode
  renderPreviewRows: (rows: SessionInfo[], id: string) => React.ReactNode
}

/** The one-way presentation boundary: every body's input remains the flat base node and base maps. */
export function PresentationOverviewList(props: Props) {
  const {
    projects,
    hidden,
    previews,
    activeProjectId,
    onEnter,
    onNewSession,
    onNewSessionSplit,
    onReorder,
    sensors,
    showAllSessions,
    renderRows,
    renderPreviewRows
  } = props

  const windowOwner = useStore($activeConnectionId)
  const home = projects[0]?.isNoProject ? projects[0] : undefined
  const groups = groupProjects(home ? projects.slice(1) : projects, windowOwner ?? undefined)
  const draggable = groups.length > 1 && !!onReorder
  const Row = draggable ? SortableProjectOverviewRow : ProjectOverviewRow
  const GroupRow = draggable ? SortableProjectGroupRow : ProjectGroupRow

  const projectRow = (
    project: SidebarProjectTree,
    Component: typeof ProjectOverviewRow,
    presentation?: React.ComponentProps<typeof ProjectOverviewRow>['presentation']
  ) => (
    <Component
      activeProjectId={activeProjectId}
      hiddenSessionCount={hidden?.counts[project.id]}
      isSessionHidden={hidden?.isHidden}
      key={project.id}
      onEnter={onEnter}
      onNewSession={onNewSession}
      onNewSessionSplit={onNewSessionSplit}
      presentation={presentation}
      previewSessions={previews?.[project.id]}
      project={project}
      renderRows={showAllSessions ? items => renderPreviewRows(items, project.id) : renderRows}
    />
  )

  const rows = groups.map(group =>
    group.members.length === 1 ? (
      projectRow(group.primary, Row)
    ) : (
      <GroupRow
        activeProjectId={activeProjectId}
        group={group}
        key={group.primary.id}
        onEnter={onEnter}
        onNewSession={onNewSession}
        onNewSessionSplit={onNewSessionSplit}
        renderMember={(project, presentation) => projectRow(project, ProjectOverviewRow, presentation)}
      />
    )
  )

  return (
    <>
      {home && projectRow(home, ProjectOverviewRow)}
      {draggable && onReorder ? (
        <ReorderableList
          ids={groupSortableIds(groups)}
          onReorder={ids => onReorder(expandGroupOrder(groups, ids))}
          sensors={sensors}
        >
          {rows}
        </ReorderableList>
      ) : (
        rows
      )}
    </>
  )
}

function SortableProjectOverviewRow(props: React.ComponentProps<typeof ProjectOverviewRow>) {
  return <ProjectOverviewRow {...props} {...useSortableBindings(props.project.id)} />
}

function SortableProjectGroupRow(props: React.ComponentProps<typeof ProjectGroupRow>) {
  return <ProjectGroupRow {...props} {...useSortableBindings(props.group.primary.id)} />
}
