/**
 * [daveey] Plugin-contributed sidebar sections (`sidebar.sections`), mounted
 * between Pinned and Projects, plus the section/row chrome they build from so a
 * plugin's list sits on the same grid as the core sections around it.
 */

import type * as React from 'react'

import { SIDEBAR_SECTIONS_AREA } from '@/app/routes'
import { Codicon } from '@/components/ui/codicon'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { SidebarGroup, SidebarGroupContent } from '@/components/ui/sidebar'
import { Tip } from '@/components/ui/tooltip'
import { useContributions } from '@/contrib'
import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { cn } from '@/lib/utils'

import { SidebarPanelLabel } from '../../shell/sidebar-label'

import { SidebarRowBody, SidebarRowLabel, SidebarRowLead, SidebarRowShell, SidebarSectionMeta } from './chrome'
import { SidebarLoadMoreRow } from './load-more-row'

/** Renders every `sidebar.sections` contribution in order. */
export function SidebarContributedSections({ area = SIDEBAR_SECTIONS_AREA }: { area?: string }) {
  const items = useContributions(area)

  if (!items.length) {
    return null
  }

  return (
    <>
      {items.map(item =>
        item.render ? (
          <ContribBoundary id={item.id} key={item.id} variant="chip">
            <ContribRender render={item.render} />
          </ContribBoundary>
        ) : null
      )}
    </>
  )
}

export interface SidebarSectionProps {
  label: string
  /** Muted count beside the label. */
  count?: number
  open: boolean
  onToggle: () => void
  children?: React.ReactNode
  /** Rows hidden behind the cap; > 0 shows the load-more control. */
  hiddenCount?: number
  onLoadMore?: () => void
  loadMoreStep?: number
}

/** Collapsible sidebar section — the header and body geometry of the core
 *  Cron / messaging sections. */
export function SidebarSection({
  children,
  count,
  hiddenCount = 0,
  label,
  loadMoreStep = 10,
  onLoadMore,
  onToggle,
  open
}: SidebarSectionProps) {
  return (
    <SidebarGroup className="shrink-0 p-0 pb-1">
      <div className="group/section flex shrink-0 items-center justify-between pb-1 pt-1.5">
        <button
          aria-expanded={open}
          className="group/section-label flex w-fit min-w-0 items-center gap-1 bg-transparent text-left leading-none"
          onClick={onToggle}
          type="button"
        >
          <SidebarPanelLabel>{label}</SidebarPanelLabel>
          {count !== undefined && <SidebarSectionMeta>{count}</SidebarSectionMeta>}
          <DisclosureCaret
            className="text-(--ui-text-tertiary) opacity-0 transition group-hover/section-label:opacity-100"
            open={open}
          />
        </button>
      </div>
      {open && (
        <SidebarGroupContent className="scrollbar-fade flex max-h-72 flex-col gap-px overflow-x-hidden overflow-y-auto overscroll-contain pb-1.75 compact:max-h-none compact:overflow-visible">
          {children}
          {hiddenCount > 0 && onLoadMore && (
            <SidebarLoadMoreRow onClick={onLoadMore} step={Math.min(loadMoreStep, hiddenCount)} />
          )}
        </SidebarGroupContent>
      )}
    </SidebarGroup>
  )
}

export interface SidebarSectionRowProps {
  label: string
  onSelect: () => void
  /** Hover tooltip (defaults to the label). */
  tip?: React.ReactNode
  /** Leading codicon; omitted = a small dot. */
  icon?: string
  /** CSS color for the leading icon/dot. */
  accentColor?: string
  /** Muted trailing text (age, owner). */
  meta?: React.ReactNode
  active?: boolean
  /** Nest under a group header row (one lead-column step in). */
  indent?: boolean
  /** Muted label typography for group header rows. */
  muted?: boolean
  /** Hover-revealed trailing controls (e.g. an archive button); replace
   *  `meta` while the row is hovered or focused. Clicks inside should
   *  `stopPropagation()` — they sit outside the row body anyway. */
  actions?: React.ReactNode
  /** Emphasised (unread) label typography. */
  strong?: boolean
}

/** One clickable row on the session-row grid. */
export function SidebarSectionRow({
  accentColor,
  actions,
  active,
  icon,
  indent,
  label,
  meta,
  muted,
  onSelect,
  strong,
  tip
}: SidebarSectionRowProps) {
  const metaNode = meta ? (
    <span
      className={cn(
        'text-[0.6875rem] text-(--ui-text-tertiary) tabular-nums',
        actions && 'group-focus-within/contrib-row:hidden group-hover/contrib-row:hidden'
      )}
    >
      {meta}
    </span>
  ) : null

  const actionsNode = actions ? (
    <span
      className={cn(
        'items-center gap-0.5',
        meta ? 'hidden group-focus-within/contrib-row:flex group-hover/contrib-row:flex' : 'flex opacity-0 group-focus-within/contrib-row:opacity-100 group-hover/contrib-row:opacity-100'
      )}
      data-slot="sidebar-section-row-actions"
    >
      {actions}
    </span>
  ) : null

  return (
    <SidebarRowShell
      actions={
        metaNode || actionsNode ? (
          <>
            {metaNode}
            {actionsNode}
          </>
        ) : undefined
      }
      className={cn(
        'group/contrib-row relative hover:bg-(--chrome-action-hover)',
        active && 'bg-(--chrome-action-hover)',
        indent && 'pl-3'
      )}
    >
      <Tip label={tip ?? label}>
        <SidebarRowBody
          className="focus-visible:bg-(--chrome-action-hover) focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
          onClick={onSelect}
        >
          <SidebarRowLead style={accentColor ? { color: accentColor } : undefined}>
            {icon ? (
              <Codicon name={icon} size="0.75rem" />
            ) : (
              <span
                aria-hidden="true"
                className="size-1 rounded-full bg-current"
                style={accentColor ? undefined : { color: 'var(--ui-text-quaternary)' }}
              />
            )}
          </SidebarRowLead>
          <SidebarRowLabel
            className={cn(
              'group-hover/contrib-row:text-foreground',
              muted && 'text-(--ui-text-tertiary)',
              strong && 'font-semibold text-foreground'
            )}
          >
            {label}
          </SidebarRowLabel>
        </SidebarRowBody>
      </Tip>
    </SidebarRowShell>
  )
}
