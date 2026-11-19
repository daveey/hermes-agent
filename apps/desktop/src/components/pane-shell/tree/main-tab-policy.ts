import type { Contribution } from '@/contrib/types'

import type { GroupNode } from './model'

/** Visibility eligibility shared by store/keyboard policy. */
export function shownGroupPanes(
  group: { active?: string; panes: readonly string[] },
  registered: readonly Contribution[],
  hidden: ReadonlySet<string>,
  narrow: boolean,
  single: boolean
): string[] {
  const paneFor = (id: string) => registered.find(pane => pane.id === id)

  const shown = group.panes.filter(id => {
    const pane = paneFor(id)
    const data = pane?.data as { collapsible?: boolean } | undefined

    return Boolean(pane) && !hidden.has(id) && !(narrow && data?.collapsible)
  })

  return singleMainTabPanes(
    { active: group.active ?? '', panes: [...group.panes] },
    shown,
    id => (paneFor(id)?.data as { placement?: string } | undefined)?.placement === 'main',
    single
  )
}

/**
 * Single is a presentation policy, not a session close: background sessions,
 * browser guests and the workspace anchor must keep their identity/lifecycle.
 * Every tree writer (adoption, restore, drops and explicit opens) resolves its
 * main slot here. Standing side/tool panes remain independently accessible.
 */
export function singleMainTabPanes(
  group: Pick<GroupNode, 'active' | 'panes'>,
  shown: readonly string[],
  isMain: (id: string) => boolean,
  single: boolean
): string[] {
  if (!single) {
    return [...shown]
  }

  const main = shown.filter(isMain)
  const retained = main.includes(group.active) ? group.active : main[0]

  return shown.filter(id => !isMain(id) || id === retained)
}
