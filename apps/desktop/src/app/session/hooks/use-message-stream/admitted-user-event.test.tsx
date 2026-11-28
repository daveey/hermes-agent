import type { GatewayEvent } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { recordSessionEventScope } from '@/store/session-states'

import { renderMessageStream } from './test-harness'

const sid = 'admission-runtime'

const payload = {
  stored_session_id: 'stored-admission',
  profile: 'work',
  message_uid: 'decision-uid',
  user_row_id: 42,
  text: 'approved exact scope',
  timestamp: 20
}

const event = {
  type: 'message.admitted',
  session_id: sid,
  connectionId: 'owner',
  profile: 'work',
  payload
} as GatewayEvent

function mount() {
  recordSessionEventScope({ session_id: sid, connectionId: 'owner', profile: 'work' })
  const hydrate = vi.fn(async () => undefined)

  const stream = renderMessageStream(sid, {
    states: new Map([[sid, createClientSessionState('stored-admission')]]),
    hydrateFromStoredSession: hydrate
  })

  return { stream, hydrate }
}

afterEach(() => {
  cleanup()
  _resetSessionOwnerHintsForTests({ storage: true })
})

describe('authoritative admitted user row', () => {
  it('paints before completion once, retains live output on redelivery and hydration', () => {
    const { stream, hydrate } = mount()
    act(() => stream.handleEvent(event))
    expect(stream.state().messages.map(m => m.role)).toEqual(['user'])
    expect(stream.state().messages[0].rowId).toBe(42)
    act(() => {
      stream.handleEvent({ type: 'message.start', session_id: sid, payload: {} })
      stream.handleEvent({ type: 'message.delta', session_id: sid, payload: { text: 'live answer', timestamp: 21 } })
      stream.handleEvent({ type: 'message.interim', session_id: sid, payload: { text: 'live answer', timestamp: 21 } })
    })
    const before = stream.state().messages
    act(() => stream.handleEvent(event))
    expect(stream.state().messages).toBe(before)
    expect(stream.text()).toBe('live answer')
    expect(hydrate).not.toHaveBeenCalled()

    // A storage refresh may use a different display ID for the same durable row.
    stream.states.set(sid, {
      ...stream.state(),
      messages: toChatMessages([{ role: 'user', content: payload.text, row_id: 42, timestamp: 20 }])
    })
    act(() => stream.handleEvent(event))
    expect(stream.state().messages).toHaveLength(1)
  })

  it('inserts a delayed row ahead of live output without replacing any assistant parts', () => {
    const { stream } = mount()

    const assistant = {
      id: 'live',
      role: 'assistant' as const,
      timestamp: 21,
      pending: true,
      parts: [{ type: 'text' as const, text: 'delta kept' }]
    }

    stream.states.set(sid, { ...stream.state(), busy: true, turnLive: true, streamId: 'live', messages: [assistant] })
    act(() => stream.handleEvent(event))
    expect(stream.state().messages.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(stream.state().messages[1]).toBe(assistant)
    expect(stream.state().streamId).toBe('live')
    expect(stream.state().busy).toBe(true)
  })

  it('keeps distinct admitted rows even when their text is identical', () => {
    const { stream } = mount()
    act(() => {
      stream.handleEvent(event)
      stream.handleEvent({
        ...event,
        payload: {
          ...payload,
          user_row_id: 43,
          message_uid: 'another-decision',
          timestamp: 22
        }
      })
    })
    expect(stream.state().messages.map(message => message.rowId)).toEqual([42, 43])
  })

  it('updates only the bound background session, not the foreground', () => {
    const background = 'admission-background'
    recordSessionEventScope({ session_id: background, connectionId: 'owner', profile: 'work' })
    const foreground = createClientSessionState('foreground-stored')

    const stream = renderMessageStream('foreground-runtime', {
      states: new Map([
        ['foreground-runtime', foreground],
        [background, createClientSessionState(payload.stored_session_id)]
      ])
    })

    act(() => stream.handleEvent({ ...event, session_id: background }))
    expect(stream.state(background).messages).toHaveLength(1)
    expect(stream.state()).toBe(foreground)
  })

  it('accepts a target profile distinct from its owner socket profile', () => {
    const runtime = 'admission-multiplexed'
    setSessionOwnerHint(runtime, { connectionId: 'owner', profile: 'default', targetProfile: 'work' })

    const stream = renderMessageStream(runtime, {
      states: new Map([[runtime, createClientSessionState(payload.stored_session_id)]])
    })

    act(() => stream.handleEvent({ ...event, session_id: runtime, profile: 'default' }))
    expect(stream.state().messages).toHaveLength(1)
  })

  it('rejects a known runtime with no bound owner', () => {
    const runtime = 'admission-unbound'

    const stream = renderMessageStream(runtime, {
      states: new Map([[runtime, createClientSessionState(payload.stored_session_id)]])
    })

    act(() => stream.handleEvent({ ...event, session_id: runtime }))
    expect(stream.state().messages).toEqual([])
  })

  it.each([
    { session_id: undefined },
    { session_id: 'unknown-runtime' },
    { connectionId: 'foreign' },
    { profile: 'foreign' },
    { payload: { ...payload, profile: 'foreign' } },
    { payload: { ...payload, stored_session_id: 'other-stored' } }
  ])('rejects foreign or unbound identity %j', override => {
    const { stream } = mount()
    act(() => stream.handleEvent({ ...event, ...override } as GatewayEvent))
    expect(stream.state().messages).toEqual([])
    expect(stream.states.size).toBe(1)
  })
})
