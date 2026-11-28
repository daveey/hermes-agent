import type { GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { persistInFlightTurnState, resetInFlightTurnJournalStateForTests } from '@/lib/inflight-turn-journal'
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

// A window reloads while a reply streams. The canonical session.resume cuts a
// snapshot of the streaming segment (`inflight`, with its `segment_stamp`
// offsets) when it subscribes the new socket, so deltas published after that
// cut can reach the renderer before the resume answer does. Those deltas and
// the snapshot are two halves of one segment: the row must hold both, merged
// by their stamped offsets, never the snapshot alone (which dropped them until
// message.complete repaired the text) and never a copy of a re-sent range.
const storedId = 'reload-stream-target'
const runtimeId = 'reload-runtime'
const noop = async () => undefined

const storedRows: SessionMessage[] = [
  { id: 1, role: 'user', content: 'U1 first', timestamp: 10 },
  { id: 2, role: 'assistant', content: 'A1 one', timestamp: 11 },
  { id: 3, role: 'user', content: 'U2 second', timestamp: 20 }
]

function mount(snapshot: SessionResumeResult, gate: Promise<void>) {
  const requestGateway = vi.fn().mockImplementation(async () => {
    await gate

    return snapshot
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

/** The window before the reload journaled its live row (throttled write). */
async function journalLiveRowBeforeReload() {
  const live: ChatMessage[] = [
    ...toChatMessages(storedRows),
    { id: 'assistant-stream-before-reload', role: 'assistant', parts: [{ type: 'text', text: 'A2 ' }], pending: true }
  ]

  persistInFlightTurnState({
    awaitingResponse: false,
    busy: true,
    messages: live,
    storedSessionId: storedId,
    streamId: 'assistant-stream-before-reload',
    turnStartedAt: 19_000
  })
  await vi.advanceTimersByTimeAsync(500)
}

const turn = { turn_id: 'turn-2', segment: 0 }

/** Snapshot cut after `A2 TAIL ` (UTF-16 [0, 8)), the text the backend streamed so far. */
const snapshotInflight = {
  user: '',
  assistant: 'A2 TAIL ',
  streaming: true,
  segment_only: true,
  segment_stamp: { ...turn, offset: 0, end: 8 }
} as const

async function resumeWithEarlyDeltas(
  sessionId: string,
  early: { offset: number; text: string }[],
  { flushBeforeAnswer, journaled = true }: { flushBeforeAnswer: boolean; journaled?: boolean }
) {
  if (journaled) {
    await journalLiveRowBeforeReload()
  }

  let answer!: () => void
  const gate = new Promise<void>(resolve => (answer = resolve))

  const { result } = mount(
    {
      session_id: sessionId,
      resumed: storedId,
      messages: storedRows,
      message_count: 3,
      running: true,
      turn_started_at: 19,
      inflight: snapshotInflight
    } as SessionResumeResult,
    gate
  )

  const send = (type: GatewayEvent['type'], payload: GatewayEvent['payload']) =>
    act(() => result.current.stream.handleGatewayEvent({ session_id: sessionId, type, payload } as GatewayEvent))

  let resumed!: Promise<unknown>
  await act(async () => {
    resumed = result.current.actions.resumeSession(storedId, true)
    await vi.advanceTimersByTimeAsync(50)
  })

  // Published after the snapshot's cut: they reach the new socket while the resume RPC is still in flight.
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
    const rows = result.current.cache.sessionStateByRuntimeIdRef.current.get(sessionId)!.messages
    const prompt = rows.findLastIndex(row => row.role === 'user')

    return rows
      .slice(prompt + 1)
      .filter(row => row.role === 'assistant')
      .map(row => chatMessageText(row))
  }

  return { replies, send }
}

describe.each([
  ['the stored id', storedId],
  ['a new runtime id', runtimeId]
] as const)('a resume answered after early stamped deltas (runtime bound under %s)', (_label, sessionId) => {
  it.each([
    ['painted before the answer', true],
    ['still buffered at the answer', false]
  ] as const)('keeps a delta past the snapshot %s, and the next delta extends the same row', async (_when, flush) => {
    const { replies, send } = await resumeWithEarlyDeltas(sessionId, [{ offset: 8, text: 'NEXT ' }], {
      flushBeforeAnswer: flush
    })

    expect(replies()).toEqual(['A2 TAIL NEXT '])

    send('message.delta', { text: 'MORE', ...turn, offset: 13 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150)
    })

    // Before message.complete: the live row alone must already hold the whole segment.
    expect(replies()).toEqual(['A2 TAIL NEXT MORE'])
  })

  it.each([
    ['painted before the answer', true],
    ['still buffered at the answer', false]
  ] as const)('keeps a delta past the snapshot %s with no journaled row (app restart)', async (_when, flush) => {
    const { replies, send } = await resumeWithEarlyDeltas(sessionId, [{ offset: 8, text: 'NEXT ' }], {
      flushBeforeAnswer: flush,
      journaled: false
    })

    expect(replies()).toEqual(['A2 TAIL NEXT '])

    send('message.delta', { text: 'MORE', ...turn, offset: 13 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150)
    })

    expect(replies()).toEqual(['A2 TAIL NEXT MORE'])
  })

  it('keeps only the unseen part of an early delta that overlaps the snapshot', async () => {
    const { replies } = await resumeWithEarlyDeltas(sessionId, [{ offset: 3, text: 'TAIL NEXT ' }], {
      flushBeforeAnswer: true
    })

    expect(replies()).toEqual(['A2 TAIL NEXT '])
  })

  it('adds nothing for an early delta the snapshot already covers', async () => {
    const { replies, send } = await resumeWithEarlyDeltas(sessionId, [{ offset: 3, text: 'TAIL ' }], {
      flushBeforeAnswer: true
    })

    expect(replies()).toEqual(['A2 TAIL '])

    send('message.delta', { text: 'NEXT', ...turn, offset: 8 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150)
    })

    expect(replies()).toEqual(['A2 TAIL NEXT'])
  })

  it('drops a replayed copy of the snapshot range that arrives after the answer', async () => {
    const { replies, send } = await resumeWithEarlyDeltas(sessionId, [], { flushBeforeAnswer: true })

    expect(replies()).toEqual(['A2 TAIL '])

    send('message.delta', { text: 'TAIL ', ...turn, offset: 3 })
    send('message.delta', { text: 'NEXT', ...turn, offset: 8 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150)
    })

    expect(replies()).toEqual(['A2 TAIL NEXT'])
  })
})
