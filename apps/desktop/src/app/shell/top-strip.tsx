import { useLayoutEffect, useRef, useState } from 'react'

import { LayoutTreeRoot } from '@/components/pane-shell/tree/renderer'
import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { TOP_STRIP_AREA } from '@/sdk/areas'

import { useTitlebarHeight } from './use-touch-titlebar'

/** Shell-wide chrome stays outside every pane/zone. With a strip, reserve the
 * titlebar once here instead of letting top-edge zones merge their tabs into
 * it; all tabs then start below the strip. Without one, keep the old geometry. */
export function ShellWorkspace() {
  const items = useContributions(TOP_STRIP_AREA).filter(item => item.render)
  const titlebarHeight = useTitlebarHeight()
  const stripRef = useRef<HTMLDivElement>(null)
  const [hasContent, setHasContent] = useState(false)
  const hasItems = items.length > 0
  const hasStrip = hasItems && hasContent

  useLayoutEffect(() => {
    const strip = stripRef.current

    if (!strip) {
      return
    }

    // Keep contributors mounted even at zero height: their own state can reveal
    // content without changing the registry. Observe natural height, not count.
    const measure = () => setHasContent(strip.getBoundingClientRect().height > 0)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(strip)

    return () => observer.disconnect()
  }, [hasItems])

  return (
    <>
      {hasStrip && (
        <div
          aria-hidden="true"
          className="w-full shrink-0 [-webkit-app-region:drag]"
          data-shell-titlebar-band=""
          key="titlebar"
          style={{ height: titlebarHeight }}
        />
      )}
      {hasItems && (
        <div
          className="flex w-full min-w-0 shrink-0 flex-col [-webkit-app-region:no-drag]"
          data-shell-top-strip=""
          key="strip"
          ref={stripRef}
        >
          {items.map(item => (
            <ContribBoundary id={item.id} key={`${item.source ?? 'core'}:${item.id}`} variant="chip">
              <ContribRender render={item.render!} />
            </ContribBoundary>
          ))}
        </div>
      )}
      <LayoutTreeRoot key="workspace" titlebar={!hasStrip} />
    </>
  )
}
