import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { withNoReplyNotice } from '@/store/session-no-reply'

import { renderMessageStream } from './test-harness'

const SID = 'quiet-runtime'

afterEach(cleanup)

describe('late completion after a no-reply notice', () => {
  it('keeps the notice on an empty terminal frame', async () => {
    const messages = withNoReplyNotice([
      { id: 'prompt', role: 'user', parts: [{ type: 'text', text: 'resume' }] },
      { id: 'stream', role: 'assistant', parts: [], pending: false }
    ])

    const stream = renderMessageStream(SID, {
      states: new Map([[SID, { ...createClientSessionState(), messages, heartbeatSettledStreamId: 'stream' }]])
    })

    await act(() => stream.handleEvent({ type: 'message.complete', session_id: SID, payload: { text: '' } }))
    expect(stream.state().messages.filter(message => message.error)).toHaveLength(1)
    expect(stream.state().messages.at(-1)?.errorSurface?.code).toBe('no_reply')
  })

  it('does not erase an unrelated failure when completing a bubble', async () => {
    const stream = renderMessageStream(SID, {
      states: new Map([
        [
          SID,
          {
            ...createClientSessionState(),
            heartbeatSettledStreamId: 'stream',
            messages: [
              {
                id: 'stream',
                role: 'assistant',
                parts: [],
                error: 'Provider failed',
                errorSurface: { code: 'timeout', layer: 'provider', retryable: true }
              }
            ]
          }
        ]
      ])
    })

    await act(() =>
      stream.handleEvent({ type: 'message.complete', session_id: SID, payload: { text: 'Partial reply' } })
    )
    expect(stream.state().messages.at(-1)?.error).toBe('Provider failed')
  })

  it.each([true, false])('settles one reply with retained stream identity=%s', async retained => {
    const messages = withNoReplyNotice([
      { id: 'prompt', role: 'user', parts: [{ type: 'text', text: 'resume' }] },
      { id: 'stream', role: 'assistant', parts: [], pending: false }
    ])

    const stream = renderMessageStream(SID, {
      states: new Map([
        [
          SID,
          {
            ...createClientSessionState(),
            messages,
            heartbeatSettledStreamId: retained ? 'stream' : null
          }
        ]
      ])
    })

    await act(() =>
      stream.handleEvent({
        type: 'message.complete',
        session_id: SID,
        payload: { text: 'Queued the next stage.' }
      })
    )

    const replies = stream.state().messages.filter(message => message.role === 'assistant')
    expect(replies).toHaveLength(1)
    expect(replies[0].error).toBeUndefined()
    expect(replies[0].errorSurface).toBeUndefined()
    expect(replies[0].parts).toMatchObject([{ type: 'text', text: 'Queued the next stage.' }])
  })
})
