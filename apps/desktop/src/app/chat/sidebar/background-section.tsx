import { useStore } from '@nanostores/react'
import { type ComponentProps, useMemo } from 'react'

import { SIDEBAR_SECTIONS_TOP_AREA } from '@/app/routes'
import { useI18n } from '@/i18n'
import { Codecs, persistentAtom } from '@/lib/persisted'

import { CardCountsContext } from './card-counts-context'
import { SidebarContributedSections } from './contributed-sections'
import { ownerChatPolicy } from './owner-chats'
import { BackgroundCardSquares } from './session-row-card-squares'
import { SidebarSessionsSection } from './sessions-section'

const $backgroundOpen = persistentAtom('hermes.desktop.sidebarBackgroundOpen', true, Codecs.bool)

/** Keep the complete pinned input in ChatSidebar's identity exclusion set.
 * Only the presentation splits: backend owner pins still protect idle chats. */
export function SidebarPinnedAndBackgroundSections(props: ComponentProps<typeof SidebarSessionsSection>) {
  const { t } = useI18n()
  const open = useStore($backgroundOpen)
  const { pinned, background } = useMemo(() => ownerChatPolicy.partition(props.sessions), [props.sessions])

  return (
    <>
      <SidebarContributedSections area={SIDEBAR_SECTIONS_TOP_AREA} />
      <SidebarSessionsSection {...props} sessions={pinned} sortable={pinned.length > 1} />
      {background.length > 0 && (
        <CardCountsContext value>
          <SidebarSessionsSection
            {...props}
            emptyState={null}
            headerAction={
              <div className="flex min-w-0 max-w-[calc(100%-7rem)] justify-end pr-2" data-background-counts>
                <BackgroundCardSquares sessions={background} />
              </div>
            }
            label={t.sidebar.background}
            labelMeta={background.length}
            onReorderSessions={undefined}
            onToggle={() => $backgroundOpen.set(!open)}
            open={open}
            preserveOrder
            sessions={background}
            sortable={false}
          />
        </CardCountsContext>
      )}
    </>
  )
}
