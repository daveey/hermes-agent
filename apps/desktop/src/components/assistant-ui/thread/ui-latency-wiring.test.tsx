import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import { onGatewayEvent } from '@/contrib/events'
import { peekUiLatency, resetUiLatencyForTests, startUiLatency } from '@/lib/ui-latency'

import { stubThreadEnvironment, stubThreadViewportSize } from '../test-utils'

import { TranscriptWindowProvider } from './transcript-window'

import { Thread } from '.'

stubThreadEnvironment()
stubThreadViewportSize()

const createdAt = new Date('2026-08-01T00:00:00.000Z')

const transcript = (key: string): ThreadMessage[] => [
  {
    id: `u-${key}`,
    role: 'user',
    content: [{ type: 'text', text: `hello ${key}` }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  } as ThreadMessage,
  {
    id: `a-${key}`,
    role: 'assistant',
    content: [{ type: 'text', text: `reply ${key}` }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage
]

function Pane({
  loading,
  messages,
  stored,
  visible = true
}: {
  loading?: 'session'
  messages: ThreadMessage[]
  stored: null | string
  visible?: boolean
}) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({ isRunning: false, messages, onNew: async () => {} })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TranscriptWindowProvider value={{ expandWindow: () => {}, olderAvailable: false }}>
        <PaneVisibleContext.Provider value={visible}>
          <Thread loading={loading} sessionId="runtime-1" sessionKey={stored} storedSessionId={stored} />
        </PaneVisibleContext.Provider>
      </TranscriptWindowProvider>
    </AssistantRuntimeProvider>
  )
}

let events: Array<Record<string, unknown>> = []

let off = () => {}
const settle = (ms = 60) => act(async () => void (await new Promise(resolve => setTimeout(resolve, ms))))

beforeEach(() => {
  events = []
  off = onGatewayEvent('desktop.ui_latency', event => events.push({ ...(event.payload as Record<string, unknown>) }))
})

afterEach(() => {
  cleanup()
  off()
  resetUiLatencyForTests()
})

describe('ui.session_switch wiring through the real Thread', () => {
  it('ends the span for the session whose transcript committed, after a paint', async () => {
    startUiLatency('ui.session_switch', { gateway: 'g', session_id: 'b' })
    const view = render(<Pane loading="session" messages={[]} stored="b" />)

    await settle()
    expect(events).toEqual([]) // selected, still loading: nothing to have painted

    view.rerender(<Pane messages={transcript('b')} stored="b" />)
    expect(events).toEqual([]) // committed, not yet painted

    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ gateway: 'g', name: 'ui.session_switch', session_id: 'b' })
    expect(events[0].duration_ms).toBeGreaterThanOrEqual(0)
  })

  it('a loaded, genuinely empty transcript is a real result and ends the span', async () => {
    startUiLatency('ui.session_switch', { session_id: 'e' })
    render(<Pane messages={[]} stored="e" />)

    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ name: 'ui.session_switch', session_id: 'e' })
  })

  it('does not end a switch to B on the transcript of A, then ends it when B commits', async () => {
    const view = render(<Pane messages={transcript('a')} stored="a" />)
    await settle()
    startUiLatency('ui.session_switch', { session_id: 'b' })
    view.rerender(<Pane messages={transcript('a')} stored="a" />)
    await settle()
    expect(events).toEqual([])
    expect(peekUiLatency('ui.session_switch')).not.toBeNull()

    view.rerender(<Pane messages={transcript('b')} stored="b" />)
    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ session_id: 'b' })
  })

  it('a click on the session already on screen still ends (the epoch re-checks what is visible)', async () => {
    render(<Pane messages={transcript('a')} stored="a" />)
    await settle()

    startUiLatency('ui.session_switch', { session_id: 'a' })

    await waitFor(() => expect(events).toHaveLength(1))
    expect(events[0]).toMatchObject({ session_id: 'a' })
  })

  it('never ends while the loader is up or the pane is hidden', async () => {
    startUiLatency('ui.session_switch', { session_id: 'b' })
    const view = render(<Pane loading="session" messages={transcript('b')} stored="b" />)
    await settle()
    expect(events).toEqual([])

    view.rerender(<Pane messages={transcript('b')} stored="b" visible={false} />)
    await settle()
    expect(events).toEqual([])
    expect(peekUiLatency('ui.session_switch')).not.toBeNull()
  })

  it('does not emit when the pane starts loading again before the second frame', async () => {
    startUiLatency('ui.session_switch', { session_id: 'b' })
    const view = render(<Pane messages={transcript('b')} stored="b" />)
    view.rerender(<Pane loading="session" messages={[]} stored="b" />) // reloading before any frame ran
    await settle()

    expect(events).toEqual([])
  })
})
