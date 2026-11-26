import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $clarifyRequests, clearClarifyRequest } from '@/store/clarify'
import { clearAllPrompts } from '@/store/prompts'
import { resetServerRequestsForTests } from '@/store/server-requests'
import { $sessions, setSessions } from '@/store/session'
import { $sessionDotStateById, stuckReasons } from '@/store/session-dot-state'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

// ATTENTION (needs-input / red) is derived from the request that is still open, never from "some tool finished".
// An unanswered clarify must keep the session's needs-input dot through an unrelated, delayed or replayed
// tool.complete, through the settlement of an OLDER prompt, and through an update cancel + reissue; only that
// request's own answer, withdrawal or cancel takes it down. READ/UNREAD is a separate dimension (see the last block).
//
// These drive the real gateway-event handlers and read the real sidebar projection ($sessionDotStateById), with the
// stream's session state published to the same store the sidebar reads.

const SID = 'runtime-1'
const STORED = 'stored-1'

let stream: MessageStreamHarness

const row = (id: string): SessionInfo => ({ id, message_count: 3, source: 'desktop', title: id }) as SessionInfo

function mountStream() {
  const states = new Map<string, ClientSessionState>()

  stream = renderMessageStream(SID, {
    states,
    updateSessionState: (id, updater) => {
      const next = updater(states.get(id) ?? createClientSessionState())
      states.set(id, next)
      publishSessionState(id, next)

      return next
    }
  })

  // The runtime must be bound to its stored row before the sidebar can name it.
  act(() => {
    const state = { ...createClientSessionState(), busy: true, storedSessionId: STORED }
    states.set(SID, state)
    publishSessionState(SID, state)
  })
}

const dot = () => $sessionDotStateById.get()[STORED]

const clarify = (requestId: string, question = 'Go as planned?') =>
  act(
    () =>
      void stream.handleRequest(
        'clarify',
        { questions: [{ choices: ['yes', 'no'], qid: 'q0', question }], session_id: SID },
        requestId
      )
  )

const event = (type: string, payload: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
  act(() => stream.handleEvent({ payload, session_id: SID, type, ...extra } as never))

const toolRound = (id: string, name = 'terminal') => {
  event('tool.start', { name, tool_id: id, args: {} })
  event('tool.complete', { name, tool_id: id, result: 'ok' })
}

describe('an open clarify keeps needs-input until that request settles', () => {
  beforeEach(() => {
    clearClarifyRequest()
    clearAllPrompts()
    resetServerRequestsForTests()
    clearAllSessionStates()
    setSessions([row(STORED)])
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
    clearAllPrompts()
    resetServerRequestsForTests()
    clearAllSessionStates()
    setSessions([])
    expect($sessions.get()).toEqual([])
  })

  it('paints needs-input while the clarify is parked (baseline)', () => {
    mountStream()
    clarify('req-1')

    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
    expect(stuckReasons({ id: STORED }, 'needs-input')).toEqual([`needs-input:prompt runtime=${SID}`])
  })

  it('survives an unrelated tool.complete (a different tool finishing while the question is open)', () => {
    mountStream()
    clarify('req-1')

    toolRound('other-tool')

    expect($clarifyRequests.get()[SID]?.requestId).toBe('req-1')
    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
  })

  it('survives a delayed or replayed tool.complete of an earlier call', () => {
    mountStream()
    toolRound('early')
    clarify('req-1')

    // The same completion delivered again (redelivery / replay after a reconnect).
    event('tool.complete', { name: 'terminal', tool_id: 'early', result: 'ok' }, { replayed: true })
    event('tool.complete', { name: 'terminal', tool_id: 'early', result: 'ok' })

    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
  })

  it('survives the settlement of an OLDER prompt id', () => {
    mountStream()
    clarify('req-new')

    event('clarify.settled', { prompt_id: 'req-old' })
    event('request.cancel', { id: 'req-old', method: 'clarify', reason: 'interrupted' })

    expect($clarifyRequests.get()[SID]?.requestId).toBe('req-new')
    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
  })

  it('update withdrawal + reissue: the old request is cancelled, the reissued one stays red through tool traffic', () => {
    mountStream()
    clarify('req-1')

    // The update pause withdraws the question, then the resumed turn asks again under a new id.
    event('request.cancel', { id: 'req-1', method: 'clarify', reason: 'paused_for_update' })
    expect($clarifyRequests.get()[SID]).toBeUndefined()
    expect(stream.state(SID).needsInput).toBe(false)

    clarify('req-2')
    // A late cancel for the first request must not take the reissued one down.
    event('request.cancel', { id: 'req-1', method: 'clarify', reason: 'paused_for_update' })
    toolRound('resumed-tool')

    expect($clarifyRequests.get()[SID]?.requestId).toBe('req-2')
    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
  })

  it('a still-open sudo prompt keeps needs-input when an unrelated tool completes', () => {
    mountStream()
    act(() => void stream.handleRequest('sudo', { command: 'apt update', session_id: SID }, 'sudo-1'))

    toolRound('other-tool')

    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
  })

  it('no ghost red: the matching withdrawal clears it, and so does the answered clarify finishing', () => {
    mountStream()
    clarify('req-1')
    event('request.cancel', { id: 'req-1', method: 'clarify', reason: 'timeout' })

    expect(stream.state(SID).needsInput).toBe(false)
    expect(dot()).not.toBe('needs-input')

    clarify('req-2')
    // Answered: the card leaves the store, then the clarify tool's own completion lands.
    clearClarifyRequest('req-2', SID)
    toolRound('req-2', 'clarify')

    expect(stream.state(SID).needsInput).toBe(false)
    expect(dot()).not.toBe('needs-input')
  })

  it('a matching clarify.settled clears only that request', () => {
    mountStream()
    clarify('req-1')
    event('clarify.settled', { prompt_id: 'req-1' })

    expect($clarifyRequests.get()[SID]).toBeUndefined()
    expect(stream.state(SID).needsInput).toBe(false)
    expect(dot()).not.toBe('needs-input')
  })
})

describe('read/unread is a separate dimension from attention', () => {
  beforeEach(() => {
    clearClarifyRequest()
    clearAllPrompts()
    resetServerRequestsForTests()
    clearAllSessionStates()
    setSessions([{ ...row(STORED), unread: true }])
  })

  afterEach(() => {
    cleanup()
    clearClarifyRequest()
    clearAllPrompts()
    resetServerRequestsForTests()
    clearAllSessionStates()
    setSessions([])
  })

  it('an unread row with an open clarify is needs-input, and marking it read leaves it needs-input', () => {
    mountStream()
    clarify('req-1')

    expect(dot()).toBe('needs-input')

    // Opening / marking read: the backend watermark flips, nothing about the request does.
    setSessions([{ ...row(STORED), unread: false }])

    expect($clarifyRequests.get()[SID]?.requestId).toBe('req-1')
    expect(stream.state(SID).needsInput).toBe(true)
    expect(dot()).toBe('needs-input')
  })

  it('an unread row with no open request stays unread, not needs-input', () => {
    mountStream()
    act(() => {
      const idle = { ...createClientSessionState(), storedSessionId: STORED }
      stream.states.set(SID, idle)
      publishSessionState(SID, idle)
    })

    expect(stream.state(SID).needsInput).toBe(false)
    expect(dot()).not.toBe('needs-input')
  })
})
