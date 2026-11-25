import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { SESSIONS_TAB_STRIP_AREA } from '@/sdk/areas'

/** [daveey] Plugin chrome at the right end of the Sessions zone's tab strip
 *  (beside Sessions / Bots). The tab list grows from a zero basis, so the slot
 *  is capped at 65% of the strip to keep the tabs readable; past that it
 *  scrolls sideways with a hidden scrollbar. No box when empty. */
export function SessionsTabStripSlot({ panes }: { panes: readonly string[] }) {
  const items = useContributions(SESSIONS_TAB_STRIP_AREA).filter(item => item.render)

  if (!panes.includes('sessions') || items.length === 0) {
    return null
  }

  return (
    <div
      className="flex min-w-0 max-w-[65%] shrink items-center justify-end overflow-x-auto overflow-y-hidden [-webkit-app-region:no-drag] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      data-sessions-tab-strip=""
      onPointerDown={event => event.stopPropagation()}
    >
      {items.map(item => (
        <ContribBoundary id={item.id} key={`${item.source ?? 'core'}:${item.id}`} variant="chip">
          <ContribRender render={item.render!} />
        </ContribBoundary>
      ))}
    </div>
  )
}
