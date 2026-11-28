import { act, cleanup, render, waitFor } from '@testing-library/react'
import { createRef, type RefObject } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import { onGatewayEvent } from '@/contrib/events'

import { bindUiLatency, peekUiLatency, resetUiLatencyForTests, startUiLatency, UI_LATENCY_UNBOUND } from './ui-latency'
import { UiLatencyComposerProbe } from './ui-latency-probes'

let events: Array<Record<string, unknown>> = []

let off = () => {}
let laidOut = true
const settle = (ms = 40) => act(async () => void (await new Promise(resolve => setTimeout(resolve, ms))))

beforeEach(() => {
  events = []
  laidOut = true
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(performance.now()), 0)
  )
  // jsdom lays nothing out; say whether the editor has a box.
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(
    () => (laidOut ? [{}] : []) as unknown as DOMRectList
  )
  off = onGatewayEvent('desktop.ui_latency', event => events.push({ ...(event.payload as Record<string, unknown>) }))
})

afterEach(() => {
  cleanup()
  off()
  resetUiLatencyForTests()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function Composer({
  disabled = false,
  hidden = false,
  runtimeId,
  visible = true
}: {
  disabled?: boolean
  hidden?: boolean
  runtimeId?: null | string
  visible?: boolean
}) {
  const ref = createRef<HTMLDivElement>() as RefObject<HTMLDivElement | null>

  return (
    <PaneVisibleContext.Provider value={visible}>
      <div {...(hidden ? { 'data-pane-hidden': '' } : {})}>
        <div contentEditable={!disabled} data-slot="composer-input" ref={ref} />
        <UiLatencyComposerProbe disabled={disabled} editorRef={ref} runtimeId={runtimeId} />
      </div>
    </PaneVisibleContext.Provider>
  )
}

describe('ui.new_session composer finisher', () => {
  it('ends once a visible, editable, unbound composer is on screen — after the frames', async () => {
    startUiLatency('ui.new_session', {}, { runtimeId: null })
    render(<Composer runtimeId={null} />)

    expect(events).toEqual([])
    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ name: 'ui.new_session' })
  })

  it('a click while the same unbound composer is already mounted still ends (epoch re-check)', async () => {
    render(<Composer runtimeId={null} />)
    await settle()
    startUiLatency('ui.new_session', {}, { runtimeId: null })

    await waitFor(() => expect(events).toHaveLength(1))
  })

  it('ignores the composer of another session while a fresh draft is expected, then ends on the draft', async () => {
    startUiLatency('ui.new_session', {}, { runtimeId: null })
    const view = render(<Composer runtimeId="runtime-old" />)
    await settle()
    expect(events).toEqual([])

    view.rerender(<Composer runtimeId={null} />)
    await waitFor(() => expect(events).toHaveLength(1))
  })

  it('never ends on an uneditable, laid-out-nowhere or hidden-pane composer', async () => {
    startUiLatency('ui.new_session', {}, { runtimeId: null })
    const view = render(<Composer disabled runtimeId={null} />)
    await settle()
    expect(events).toEqual([])

    laidOut = false
    view.rerender(<Composer runtimeId={null} />)
    await settle()
    expect(events).toEqual([])

    laidOut = true
    view.rerender(<Composer runtimeId={null} visible={false} />)
    await settle()
    view.rerender(<Composer hidden runtimeId={null} />)
    await settle()
    expect(events).toEqual([])
    expect(peekUiLatency('ui.new_session')).not.toBeNull()
  })

  it('a tile "+" ends only on the composer of the created runtime, not before it exists', async () => {
    startUiLatency('ui.new_session', {}, { runtimeId: UI_LATENCY_UNBOUND })
    const view = render(<Composer runtimeId={null} />)
    await settle()
    expect(events).toEqual([]) // the old/draft composer is not the tile's

    bindUiLatency('ui.new_session', { session_id: 'stored-1' }, { runtimeId: 'rt-1' })
    await settle()
    expect(events).toEqual([]) // still not mounted

    view.rerender(<Composer runtimeId="rt-1" />)
    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ name: 'ui.new_session', session_id: 'stored-1' })
  })

  it('does not emit when the composer disappears before the second frame', async () => {
    startUiLatency('ui.new_session', {}, { runtimeId: null })
    render(<Composer runtimeId={null} />)
    laidOut = false // gone again before any frame ran
    await settle()

    expect(events).toEqual([])
  })
})
