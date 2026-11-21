import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useRef } from 'react'

import { type NewSessionSplitHandler, startNewSessionDrag } from '@/app/chat/new-session-drag'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $activeConnectionId } from '@/store/connections'
import type { AgentProfileRoute } from '@/store/profile'

import { SidebarGroupRow, SidebarRowGrab, SidebarRowLead, SidebarRowLink, SidebarRowNest } from '../chrome'
import { GatewayDeviceIcon } from '../gateway-device-icon'
import { gatewayLabel } from '../gateway-group-model'
import { shellOwnsPress } from '../reorderable-list'

import { ownedNewSessionHandlers } from './gateway-project-rows'
import { useWorkspaceNodeOpen } from './model'
import { projectIcon } from './overview-row'
import { ProjectGroupMenu } from './presentation-group-menu'
import { type ProjectPresentationGroup, projectPresentationOwner } from './presentation-groups'
import type { SidebarProjectTree } from './workspace-groups'
import { WorkspaceAddButton } from './workspace-header'

/** Which machine a member lives on: the registry's device glyph and label (the window's own gateway
 *  when no other owns it). */
export function MachineHeading({ project }: { project: SidebarProjectTree }) {
  const registry = useStore($connectionsRegistry)
  const activeId = useStore($activeConnectionId)
  const owner = projectPresentationOwner(project, activeId ?? registry?.primary ?? 'local')
  const id = owner ?? activeId ?? registry?.primary ?? ''

  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <GatewayDeviceIcon gatewayId={id} />
      <span className="truncate">{gatewayLabel(id, registry, id)}</span>
    </span>
  )
}

interface ProjectGroupRowProps {
  group: ProjectPresentationGroup
  activeProjectId?: null | string
  onEnter?: (id: string) => void
  onNewSession?: (path: null | string, owner?: AgentProfileRoute) => void
  onNewSessionSplit?: NewSessionSplitHandler
  /** One member's body: the existing overview row, given its display-only presentation. */
  renderMember: (
    project: SidebarProjectTree,
    presentation: { heading: React.ReactNode; hideActions: true }
  ) => React.ReactNode
  reorderable?: boolean
  dragging?: boolean
  dragHandleProps?: React.HTMLAttributes<HTMLElement>
  ref?: React.Ref<HTMLDivElement>
  style?: React.CSSProperties
}

/**
 * One shell for same-named projects: a header speaking for the primary (its icon, name, "+", the
 * summed totals) over the members' own overview rows. The header holds no body state — each member
 * keeps its rows, expansion and fetches, and stays mounted while the shell is collapsed.
 */
export function ProjectGroupRow({
  group,
  activeProjectId,
  onEnter,
  onNewSession: onNewSessionIn,
  onNewSessionSplit: onNewSessionSplitIn,
  renderMember,
  reorderable = false,
  dragging = false,
  dragHandleProps,
  ref,
  style
}: ProjectGroupRowProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const { primary, members } = group
  const [open, toggleOpen] = useWorkspaceNodeOpen(`presentation-group:${group.key}`)
  const rowRef = useRef<HTMLDivElement>(null)
  const activeId = useStore($activeConnectionId)
  const owner = projectPresentationOwner(primary, activeId ?? undefined)

  const { onNewSession, onNewSessionSplit } = ownedNewSessionHandlers(
    { ...primary, connectionId: owner ?? undefined },
    onNewSessionIn,
    onNewSessionSplitIn
  )

  const path = primary.path ?? (primary.repos ?? []).map(repo => repo.path).find(root => (root ?? '').trim()) ?? null

  const lead = reorderable ? (
    <SidebarRowGrab
      ariaLabel={s.projects.reorder(primary.label)}
      dragging={dragging}
      dragHandleProps={dragHandleProps}
      leadClassName="overflow-visible"
    >
      {projectIcon(primary)}
    </SidebarRowGrab>
  ) : (
    <SidebarRowLead>{projectIcon(primary)}</SidebarRowLead>
  )

  return (
    <div className={cn(dragging && 'relative z-10')} data-presentation-group={group.key} ref={ref} style={style}>
      <SidebarGroupRow
        actions={
          <>
            {onNewSession && (
              <WorkspaceAddButton
                label={s.newSessionIn(primary.label)}
                onClick={() => onNewSession(path)}
                onPointerDown={
                  onNewSessionSplit
                    ? event =>
                        startNewSessionDrag(
                          placement =>
                            onNewSessionSplit(placement.dir, {
                              anchor: placement.anchor,
                              before: placement.before,
                              cwd: path
                            }),
                          event,
                          { cwd: path, label: s.newSessionIn(primary.label) }
                        )
                    : undefined
                }
              />
            )}
            <ProjectGroupMenu group={group} />
          </>
        }
        className={cn(dragging && 'cursor-grabbing bg-(--ui-sidebar-surface-background)')}
        data-glass-opaque={dragging ? '' : undefined}
        label={
          <SidebarRowLink
            aria-label={s.projects.enter(primary.label)}
            labelClassName={cn(
              'hover:text-foreground hover:underline',
              members.some(member => member.id === activeProjectId) && 'text-foreground'
            )}
            onClick={() => onEnter?.(primary.id)}
          >
            {primary.label}
            <span className="ml-1.5 text-xs text-(--ui-text-tertiary)" data-presentation-count>
              {members.reduce((sum, member) => sum + member.sessionCount, 0)}
            </span>
          </SidebarRowLink>
        }
        lead={lead}
        onPointerDown={event => {
          if (
            !shellOwnsPress(event) ||
            (event.target as HTMLElement).closest('[data-reorder-handle], [data-row-actions]')
          ) {
            return
          }

          dragHandleProps?.onPointerDown?.(event)
        }}
        ref={rowRef}
        toggle={{ ariaLabel: s.projects.toggle(primary.label, !open), onToggle: toggleOpen, open }}
        totals={{
          costUsd: members.reduce((sum, member) => sum + (member.totalCostUsd ?? 0), 0),
          tokens: members.reduce((sum, member) => sum + (member.totalTokens ?? 0), 0)
        }}
      />
      <SidebarRowNest hidden={!open}>
        {members.map(member =>
          renderMember(member, {
            heading: <MachineHeading project={member} />,
            hideActions: true
          })
        )}
      </SidebarRowNest>
    </div>
  )
}
