import type * as React from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { triggerHaptic } from '@/lib/haptics'
import { cn } from '@/lib/utils'

import { SessionActionsMenu, type SessionContextMenu } from './session-actions-menu'

const ROW_ACTION_BUTTON =
  'size-5 rounded-[4px] bg-transparent text-transparent transition-colors duration-100 hover:bg-(--ui-control-active-background) hover:text-foreground focus-visible:bg-(--ui-control-active-background) focus-visible:text-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring data-[state=open]:bg-(--ui-control-active-background) data-[state=open]:text-foreground group-hover:text-(--ui-text-tertiary) [&_svg]:size-3.5!'

type MenuProps = Omit<React.ComponentProps<typeof SessionContextMenu>, 'children'>

interface RowHoverActionsProps {
  actionsLabel: string
  archiveLabel: string
  className?: string
  menu: MenuProps
}

/** [daveey] A sidebar row's hover actions: a one-click Archive beside the
 *  kebab. Both ride one cluster that lifts over the row's tail (which reserves
 *  their width, see tailHides) and yields to a hovered PR link together. */
export function RowHoverActions({ actionsLabel, archiveLabel, className, menu }: RowHoverActionsProps) {
  return (
    <span className={cn('flex items-center gap-1', className)}>
      {!menu.archived && (
        <Tip label={archiveLabel} side="top">
          <Button
            aria-label={archiveLabel}
            className={ROW_ACTION_BUTTON}
            data-session-archive
            onClick={() => {
              triggerHaptic('selection')
              menu.onArchive?.()
            }}
            // Don't park focus on a button whose row is about to disappear.
            onMouseDown={event => event.preventDefault()}
            size="icon"
            // Pointer shortcut only: keyboard users already reach Archive via
            // the kebab menu and the `session.archive` hotkey, so the row keeps
            // its single actions tab stop.
            tabIndex={-1}
            variant="ghost"
          >
            <Codicon name="archive" size="0.875rem" />
          </Button>
        </Tip>
      )}
      <SessionActionsMenu {...menu}>
        <Button
          aria-label={actionsLabel}
          className={ROW_ACTION_BUTTON}
          data-session-actions
          size="icon"
          variant="ghost"
        >
          <Codicon name="kebab-vertical" size="0.875rem" />
        </Button>
      </SessionActionsMenu>
    </span>
  )
}
