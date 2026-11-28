import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { appendLiveSessionProjection } from '@/app/session/hooks/use-session-actions/utils'
import { type ChatMessage, markQueuedPromptStarted } from '@/lib/chat-messages'
import { toRuntimeMessage } from '@/lib/chat-runtime'

import { stubThreadEnvironment, stubThreadViewportSize } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()
stubThreadViewportSize()
afterEach(cleanup)

function Harness({ messages }: { messages: ChatMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: messages.map(toRuntimeMessage),
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

it.each([true, false])(
  'renders the actual reconnect queued bubble with update timing (known: %s), clearing on start',
  async known => {
    const started = new Date(2026, 10, 5, 9, 7).getTime() / 1000

    const messages = appendLiveSessionProjection([], {
      session_id: 'held',
      queued: {
        user: 'run after update',
        queued_reason: 'core_update',
        queued_started_at: known ? started : null
      }
    })

    const view = render(<Harness messages={messages} />)
    expect(
      screen.getByText(
        known
          ? 'Waiting for a Hermes update (started 09:07), sends automatically'
          : 'Waiting for a Hermes update, sends automatically'
      )
    ).toBeTruthy()
    expect(screen.getByText('run after update')).toBeTruthy()
    view.rerender(<Harness messages={markQueuedPromptStarted(messages)} />)
    await waitFor(() => expect(screen.queryByText(/Waiting for a Hermes update/)).toBeNull())
  }
)

it('renders a held submit bubble whose id is optimistic, and clears its label on start', async () => {
  const messages: ChatMessage[] = [
    { id: 'user-input', role: 'user', parts: [{ type: 'text', text: 'held' }], queued_reason: 'core_update' }
  ]
  const view = render(<Harness messages={messages} />)
  expect(screen.getByText('Waiting for a Hermes update, sends automatically')).toBeTruthy()
  view.rerender(<Harness messages={markQueuedPromptStarted(messages)} />)
  await waitFor(() => expect(screen.queryByText(/Waiting for a Hermes update/)).toBeNull())
})

it('keeps ordinary queued bubbles free of update copy', () => {
  render(<Harness messages={appendLiveSessionProjection([], { session_id: 'ordinary', queued: { user: 'later' } })} />)
  expect(screen.getByText('later')).toBeTruthy()
  expect(screen.queryByText(/Waiting for a Hermes update/)).toBeNull()
})
