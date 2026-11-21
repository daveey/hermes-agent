import { Codicon } from '@/components/ui/codicon'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useI18n } from '@/i18n'
import { dismissAutoProject, restoreAutoProject } from '@/store/layout'
import { notify } from '@/store/notifications'

import type { ProjectPresentationGroup } from './presentation-groups'

/** A presentation dismissal never deletes any owner's project record or files. */
export function ProjectGroupMenu({
  group,
  onExitScope
}: {
  group: ProjectPresentationGroup
  onExitScope?: () => void
}) {
  const { t } = useI18n()
  const p = t.sidebar.projects

  return (
    <DropdownMenu>
      <DropdownMenuTrigger aria-label={p.menu} className="grid size-6 place-items-center">
        <Codicon name="ellipsis" size="0.875rem" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem
          onSelect={() => {
            const ids = [...group.memberIds]
            ids.forEach(dismissAutoProject)
            onExitScope?.()
            notify({
              kind: 'success',
              message: p.hiddenFromSidebar,
              durationMs: 8_000,
              action: { label: p.undoHide, onClick: () => ids.forEach(restoreAutoProject) }
            })
          }}
        >
          <Codicon name="trash" size="0.875rem" />
          {p.removeFromSidebar}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
