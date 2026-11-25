// Sidebar nav rows: the built-in list plus plugin contributions and nav
// preferences, moved out of ./index.tsx unchanged. [daveey]
import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { useContributions } from '@/contrib/react/use-contributions'
import { $interfaceMode, shownInMode } from '@/store/interface-mode'
import { applySidebarNavPrefs, SIDEBAR_NAV_PREFS_AREA } from '@/store/sidebar-nav'

import {
  ARTIFACTS_ROUTE,
  CAPABILITIES_ROUTE,
  CRON_ROUTE,
  MESSAGING_ROUTE,
  SIDEBAR_NAV_AREA,
  type SidebarNavContribution
} from '../../routes'
import type { SidebarNavItem } from '../../types'

// A row's `tier` is the one mode it belongs to (Simple keeps the setup rows,
// Advanced adds the readouts); the list filters once, nothing is passed down.
const SIDEBAR_NAV: SidebarNavItem[] = [
  {
    id: 'capabilities',
    label: '',
    icon: props => <Codicon name="symbol-misc" {...props} />,
    route: CAPABILITIES_ROUTE,
    keybindActionId: 'nav.capabilities'
  },
  {
    id: 'messaging',
    label: '',
    icon: props => <Codicon name="comment" {...props} />,
    route: MESSAGING_ROUTE,
    keybindActionId: 'nav.messaging'
  },
  // Artifacts and Scheduled jobs are outputs of running Hermes the developer
  // way; Capabilities and Messaging are how anyone sets it up.
  {
    id: 'artifacts',
    label: '',
    icon: props => <Codicon name="files" {...props} />,
    route: ARTIFACTS_ROUTE,
    keybindActionId: 'nav.artifacts',
    tier: 'advanced'
  },
  {
    id: 'cron',
    label: '',
    icon: props => <Codicon name="watch" {...props} />,
    route: CRON_ROUTE,
    keybindActionId: 'nav.cron',
    tier: 'advanced'
  }
]

export function useSidebarNavItems(): SidebarNavItem[] {
  // Contributed nav rows (plugins pairing a page with a sidebar entry) render
  // below the built-ins with the same chrome; active = at their route.
  const navContributions = useContributions(SIDEBAR_NAV_AREA)

  const contributedNav = useMemo<SidebarNavItem[]>(
    () =>
      navContributions.flatMap(c => {
        const data = c.data as Partial<SidebarNavContribution> | undefined

        if (!data?.path?.startsWith('/') || !data.label) {
          return []
        }

        const codicon = data.codicon || 'plug'

        return [
          {
            id: c.id,
            label: data.label,
            icon: (props: { className?: string }) => <Codicon name={codicon} {...props} />,
            route: data.path,
            tier: data.tier
          }
        ]
      }),
    [navContributions]
  )

  const interfaceMode = useStore($interfaceMode)

  // Nav preferences (`sidebarNav.prefs` contributions): a plugin may hide rows
  // or re-order them. Merged here, at render, from the registry — so the
  // preference never mutates the default list, and a plugin's disable/reload
  // disposes its contribution and the rows come straight back.
  const navPrefs = useContributions(SIDEBAR_NAV_PREFS_AREA)

  return useMemo(
    () => applySidebarNavPrefs([...SIDEBAR_NAV, ...contributedNav].filter(shownInMode(interfaceMode)), navPrefs),
    [contributedNav, interfaceMode, navPrefs]
  )
}
