import type { GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { resetInFlightTurnJournalStateForTests } from '@/lib/inflight-turn-journal'
import { $activeGatewayProfile } from '@/store/profile'
import {
  _resetSessionOwnerHintsForTests,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setConnection,
  setMessages,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'
import type { SessionMessage, SessionResumeResult } from '@/types/hermes'

import { useMessageStream } from './use-message-stream'
import { useSessionActions } from './use-session-actions'
import { useSessionStateCache } from './use-session-state-cache'

vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn()
}))
vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

// Re-opening a session whose runtime is still cached (the warm path): the
// cached live row stopped at `A2 ` while this window was not subscribed. The
// canonical session.activate (session.resume) cuts the segment snapshot when it
// re-subscribes, so the delta past that cut can reach the cached row before the
// activate answer. The row must end up holding both halves, merged by offset.
const storedId = 'warm-early-delta'
const runtimeId = 'warm-early-delta-runtime'
const noop = async () => undefined

const storedRows: SessionMessage[] = [
  { id: 1, role: 'user', content: 'U1 first', timestamp: 10 },
  { id: 2, role: 'assistant', content: 'A1 one', timestamp: 11 },
  { id: 3, role: 'user', content: 'U2 second', timestamp: 20 }
]

const turn = { turn_id: 'turn-2', segment: 0 }

// The canonical owner answers session.activate as session.resume with history
// (canonical-protocol.ts drops `omit_messages`); a legacy owner omits it.
const snapshotWith = (history: 'rows' | 'omitted'): SessionResumeResult =>
  ({
    session_id: runtimeId,
    resumed: storedId,
    ...(history === 'rows' ? { messages: storedRows } : { messages: [], messages_omitted: true }),
    message_count: 3,
    running: true,
    turn_started_at: 19,
    inflight: {
      user: '',
      assistant: 'A2 TAIL ',
      streaming: true,
      segment_only: true,
      segment_stamp: { ...turn, offset: 0, end: 8 }
    }
  }) as SessionResumeResult

function mount(gate: Promise<void>, snapshot: SessionResumeResult) {
  const requestGateway = vi.fn().mockImplementation(async (method: string) => {
    if (method === 'session.activate') {
      await gate

      return snapshot
    }

    return null
  })

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

    const actions = useSessionActions({
      ...cache,
      activeSessionId: null,
      selectedStoredSessionId: null,
      busyRef,
      creatingSessionRef: useRef(false),
      getRouteToken: () => 'A',
      getRoutedStoredSessionId: () => null,
      navigate: vi.fn(),
      requestGateway,
      routedSessionId: null
    })

    const stream = useMessageStream({
      ...cache,
      queryClient,
      hydrateFromStoredSession: noop,
      refreshHermesConfig: noop,
      refreshSessions: noop
    })

    return { cache, actions, stream }
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  clearAllSessionStates()
  resetInFlightTurnJournalStateForTests()
  _resetSessionOwnerHintsForTests()
  $activeGatewayProfile.set('default')
  setConnection(null)
  setMessages([])
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setBusy(false)
  setAwaitingResponse(false)
  setSessions([
    {
      id: storedId,
      title: storedId,
      source: 'desktop',
      message_count: 3,
      tool_call_count: 0,
      is_active: true,
      started_at: 1,
      last_active: 1,
      ended_at: null,
      model: null,
      preview: null,
      input_tokens: 0,
      output_tokens: 0
    }
  ])
  vi.mocked(getLatestSessionMessages).mockReset()
  vi.mocked(getLatestSessionMessages).mockResolvedValue({ session_id: storedId, messages: storedRows })
})

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  resetInFlightTurnJournalStateForTests()
  localStorage.clear()
  setSessions([])
  setMessages([])
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function activateWithEarlyDeltas(
  history: 'rows' | 'omitted',
  cachedRow: ChatMessage | null,
  early: { offset: number; text: string }[],
  { flushBeforeAnswer }: { flushBeforeAnswer: boolean }
) {
  let answer!: () => void
  const gate = new Promise<void>(resolve => (answer = resolve))
  const { result } = mount(gate, snapshotWith(history))

  act(() => {
    result.current.cache.updateSessionState(
      runtimeId,
      state => ({
        ...state,
        busy: true,
        turnLive: true,
        streamId: cachedRow?.id ?? null,
        messages: [...toChatMessages(storedRows), ...(cachedRow ? [cachedRow] : [])]
      }),
      storedId
    )
  })

  const send = (type: GatewayEvent['type'], payload: GatewayEvent['payload']) =>
    act(() => result.current.stream.handleGatewayEvent({ session_id: runtimeId, type, payload } as GatewayEvent))

  let resumed!: Promise<unknown>
  await act(async () => {
    resumed = result.current.actions.resumeSession(storedId, true)
    await vi.advanceTimersByTimeAsync(50)
  })

  for (const delta of early) {
    send('message.delta', { text: delta.text, ...turn, offset: delta.offset })
  }

  if (flushBeforeAnswer) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150)
    })
  }

  await act(async () => {
    answer()
    await resumed
    await vi.advanceTimersByTimeAsync(150)
  })

  const replies = () => {
    const rows = result.current.cache.sessionStateByRuntimeIdRef.current.get(runtimeId)!.messages
    const prompt = rows.findLastIndex(row => row.role === 'user')

    return rows
      .slice(prompt + 1)
      .filter(row => row.role === 'assistant')
      .map(row => chatMessageText(row))
  }

  return { replies, send }
}

const stampedRow: ChatMessage = {
  id: `assistant-stream-${runtimeId}`,
  role: 'assistant',
  pending: true,
  parts: [{ type: 'text', text: 'A2 ', streamSpan: { turnId: 'turn-2', segment: 0, start: 0, end: 3 } }]
}

describe.each(['rows', 'omitted'] as const)(
  'a warm activate (history %s) answered after early stamped deltas',
  history => {
    it.each([
      ['painted before the answer', true],
      ['still buffered at the answer', false]
    ] as const)('keeps the snapshot and a delta past its cut %s', async (_when, flush) => {
      const { replies, send } = await activateWithEarlyDeltas(history, stampedRow, [{ offset: 8, text: 'NEXT ' }], {
        flushBeforeAnswer: flush
      })

      expect(replies()).toEqual(['A2 TAIL NEXT '])

      send('message.delta', { text: 'MORE', ...turn, offset: 13 })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(150)
      })

      expect(replies()).toEqual(['A2 TAIL NEXT MORE'])
    })

    it('continues the snapshot row with the next delta when the cache held no live row', async () => {
      const { replies, send } = await activateWithEarlyDeltas(history, null, [], { flushBeforeAnswer: true })

      expect(replies()).toEqual(['A2 TAIL '])
      send('message.delta', { text: 'NEXT', ...turn, offset: 8 })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(150)
      })
      expect(replies()).toEqual(['A2 TAIL NEXT'])
    })

    it('takes the snapshot when no delta beat the answer', async () => {
      const { replies } = await activateWithEarlyDeltas(history, stampedRow, [], { flushBeforeAnswer: true })

      expect(replies()).toEqual(['A2 TAIL '])
    })
  }
)
