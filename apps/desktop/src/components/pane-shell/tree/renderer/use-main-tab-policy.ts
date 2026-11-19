import { useStore } from '@nanostores/react'

import type { Contribution } from '@/contrib/types'
import { $tabStripDefault } from '@/store/tabstrip-prefs'

import { shownGroupPanes } from '../main-tab-policy'
import type { GroupNode } from '../model'

/** Resolve every main-tab producer at the renderer's one visibility boundary. */
export function useMainTabPolicy(
  node: GroupNode,
  panes: readonly Contribution[],
  hidden: ReadonlySet<string>,
  narrow: boolean,
  revealsHidden: boolean
) {
  const single = useStore($tabStripDefault) === 'single'
  const hiddenPanes = revealsHidden ? new Set<string>() : hidden
  const available = shownGroupPanes(node, panes, hiddenPanes, narrow, false)
  const shown = single ? shownGroupPanes(node, panes, hiddenPanes, narrow, true) : available

  return { available, shown }
}
