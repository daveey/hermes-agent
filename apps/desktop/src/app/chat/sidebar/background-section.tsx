import { useStore } from '@nanostores/react'
import type { ComponentProps } from 'react'

import { useI18n } from '@/i18n'
import { Codecs, persistentAtom } from '@/lib/persisted'

import { CardCountsContext } from './card-counts-context'
import { ownerChatPolicy } from './owner-chats'
import { aggregateCardColours, CardSquares } from './session-row-card-squares'
import { SidebarSessionsSection } from './sessions-section'

const $backgroundOpen = persistentAtom('hermes.desktop.sidebarBackgroundOpen', true, Codecs.bool)

/** Keep the complete pinned input in ChatSidebar's identity exclusion set.
 * Only the presentation splits: backend owner pins still protect idle chats. */
export function SidebarPinnedAndBackgroundSections(props: ComponentProps<typeof SidebarSessionsSection>) {
  const { t } = useI18n()
  const open = useStore($backgroundOpen)
  const { pinned, background } = ownerChatPolicy.partition(props.sessions)

  return (
    <>
      <SidebarSessionsSection {...props} sessions={pinned} sortable={pinned.length > 1} />
      {background.length > 0 && (
        <CardCountsContext value>
          <SidebarSessionsSection
            {...props}
            emptyState={null}
            label={t.sidebar.background}
            labelMeta={
              <>
                {background.length}
                <CardSquares colours={aggregateCardColours(background)} counts />
              </>
            }
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
