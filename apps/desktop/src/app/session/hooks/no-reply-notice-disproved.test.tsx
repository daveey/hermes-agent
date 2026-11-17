import type { GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { textPart } from '@/lib/chat-messages'
import { NO_REPLY_ERROR } from '@/lib/chat-messages/no-reply'
import { createClientSessionState } from '@/lib/chat-runtime'
import { setActiveSessionId, setAwaitingResponse, setBusy, setMessages } from '@/store/session'
import { $sessionStates, clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { useMessageStream } from './use-message-stream'
import { useSessionStateCache } from './use-session-state-cache'

// The silent-turn check stamped "Hermes ended this turn without a reply" on a
// turn, and that turn then kept producing. Its own stream output is the proof
// the stamp was wrong; a frame that is not turn output proves nothing.
const runtimeId = 'stamped-runtime'
const noop = async () => undefined

function mount() {
  return renderHook(() => {
    const busyRef = useRef(false)
    const queryClient = useRef(new QueryClient()).current

    const cache = useSessionStateCache({
      activeSessionId: null,
      selectedStoredSessionId: null,
      busyRef,
      setMessages,
      setBusy,
      setAwaitingResponse
    })

    return useMessageStream({
      ...cache,
      queryClient,
      hydrateFromStoredSession: noop,
      refreshHermesConfig: noop,
      refreshSessions: noop
    })
  })
}

function stamp() {
  publishSessionState(runtimeId, {
    ...createClientSessionState('stamped-stored'),
    messages: [
      { id: 'u1', parts: [textPart('resume')], role: 'user' },
      {
        error: NO_REPLY_ERROR,
        errorSurface: { code: 'no_reply', layer: 'runtime', retryable: true },
        id: 'a-notice',
        parts: [],
        role: 'assistant'
      }
    ]
  })
}

const notice = () => $sessionStates.get()[runtimeId]?.messages.find(message => message.id === 'a-notice')

describe('a no-reply notice on a turn that is still producing', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    clearAllSessionStates()
    setActiveSessionId(null)
    setMessages([])
  })

  afterEach(() => {
    cleanup()
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
    clearAllSessionStates()
  })

  it('leaves when the turn emits stream output', () => {
    const { result } = mount()

    stamp()
    expect(notice()?.error).toBe(NO_REPLY_ERROR)

    act(() =>
      result.current.handleGatewayEvent({
        session_id: runtimeId,
        type: 'reasoning.delta',
        payload: { text: 'still thinking' }
      } as GatewayEvent)
    )

    expect(notice()).toBeUndefined()
  })

  it('stays for a frame that is not turn output', () => {
    const { result } = mount()

    stamp()

    act(() =>
      result.current.handleGatewayEvent({
        session_id: runtimeId,
        type: 'session.info',
        payload: {}
      } as GatewayEvent)
    )

    expect(notice()?.error).toBe(NO_REPLY_ERROR)
  })
})
