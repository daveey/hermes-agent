import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { MAIN_PANE_TOP_AREA } from '@/sdk/areas'

/** Zone-owned chrome, outside the active pane lifecycle. No padding, border or
 * minimum height: null-rendering guests remain mounted without reserving space. */
export function MainPaneTop({ panes }: { panes: readonly string[] }) {
  const items = useContributions(MAIN_PANE_TOP_AREA).filter(item => item.render)

  if (!panes.includes('workspace') || items.length === 0) {
    return null
  }

  return (
    <div className="flex w-full min-w-0 shrink-0 flex-col [-webkit-app-region:no-drag]" data-main-pane-top="">
      {items.map(item => (
        <ContribBoundary id={item.id} key={`${item.source ?? 'core'}:${item.id}`} variant="chip">
          <ContribRender render={item.render!} />
        </ContribBoundary>
      ))}
    </div>
  )
}
