import { registryBackendScopeKey } from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'

import { $activeSessionId, $selectedStoredSessionId, $unreadFinishedSessionIds } from './session'
import {
  $attentionSessionIds,
  $sessionStates,
  clearAllSessionStates,
  publishSessionState,
  reconcileBusyStatesOnReconnect,
  recordSessionEventScope
} from './session-states'

function state(over: Partial<ClientSessionState> = {}): ClientSessionState {
  return { ...createClientSessionState(null), storedSessionId: 's1', ...over }
}

const HUB = { connectionId: 'hub', profile: 'default' }
const HUB_SCOPE = registryBackendScopeKey(HUB.connectionId, HUB.profile)

// selfheal desktop-54ae373894 (t_01a1d3142a47913f): a clarify was pending on runtime 66877047 when the hub backend
// restarted (ws close 1012). Startup resume auto-continued the stored session on a NEW runtime (c442506d), whose turn
// ran and finished. The old runtime's `needsInput` was never retired: reconnect deliberately leaves it, the
// active_list reap only covers the active gateway's polled runtimes, and `evictable()` keeps needsInput states. So the
// stored session stayed in $attentionSessionIds → dot `needs-input` → red `stuck` stripe with no open question.
describe('a blocking prompt on a runtime the backend replaced', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    $selectedStoredSessionId.set(null)
    $unreadFinishedSessionIds.set([])
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    clearAllSessionStates()
    $activeSessionId.set(null)
  })

  it('retires when the same stored session runs a turn on a new runtime (scoped reconnect)', () => {
    recordSessionEventScope({ ...HUB, session_id: 'rt-old' })
    publishSessionState('rt-old', state({ busy: true, needsInput: true }))
    expect($attentionSessionIds.get()).toEqual(['s1'])

    reconcileBusyStatesOnReconnect(HUB_SCOPE)
    // Reconnect keeps a blocking prompt on purpose: on a same-backend blip it is still live.
    expect($attentionSessionIds.get()).toEqual(['s1'])

    recordSessionEventScope({ ...HUB, session_id: 'rt-new' })
    publishSessionState('rt-new', state({ busy: true, turnLive: true }))
    publishSessionState('rt-new', state({ busy: false }))

    expect($attentionSessionIds.get()).toEqual([])
    expect($sessionStates.get()['rt-old']?.needsInput).not.toBe(true)
  })

  it('retires on the primary socket too, without any active_list poll', () => {
    publishSessionState('rt-old', state({ busy: true, needsInput: true }))
    reconcileBusyStatesOnReconnect()

    publishSessionState('rt-new', state({ busy: true }))

    expect($attentionSessionIds.get()).toEqual([])
  })

  it('keeps the prompt when the new runtime belongs to another stored session', () => {
    publishSessionState('rt-old', state({ needsInput: true })) // as reconnect leaves it
    publishSessionState('rt-other', state({ busy: true, storedSessionId: 's2' }))

    expect($attentionSessionIds.get()).toEqual(['s1'])
  })

  it('keeps the prompt when a sibling runtime only appears idle (no turn proves the old one dead)', () => {
    publishSessionState('rt-old', state({ needsInput: true })) // as reconnect leaves it
    publishSessionState('rt-new', state({ busy: false }))

    expect($attentionSessionIds.get()).toEqual(['s1'])
  })

  it('keeps a prompt raised by the running runtime itself', () => {
    publishSessionState('rt-old', state({ busy: true, needsInput: true }))
    publishSessionState('rt-new', state({ needsInput: true, turnLive: true }))

    expect($attentionSessionIds.get()).toEqual(['s1'])
    expect($sessionStates.get()['rt-new']?.needsInput).toBe(true)
  })

  it('leaves a sibling that still claims a turn to its own terminal events', () => {
    publishSessionState('rt-busy', state({ busy: true, needsInput: true }))
    publishSessionState('rt-awaiting', state({ awaitingResponse: true, needsInput: true }))
    publishSessionState('rt-new', state({ busy: true }))

    expect($sessionStates.get()['rt-busy']?.needsInput).toBe(true)
    expect($sessionStates.get()['rt-awaiting']?.needsInput).toBe(true)
    expect($attentionSessionIds.get()).toEqual(['s1'])
  })

  // PR #1878 review: a sibling whose turn is live (turnLive without a busy claim) is still running.
  it('leaves a sibling whose turn is live to its own terminal events', () => {
    publishSessionState('rt-live', state({ needsInput: true, turnLive: true }))
    publishSessionState('rt-new', state({ busy: true }))

    expect($sessionStates.get()['rt-live']?.needsInput).toBe(true)
    expect($attentionSessionIds.get()).toEqual(['s1'])
  })

  // PR #1892 review: a sibling left turn-live (after Stop, before its terminal event) when the backend restarted
  // never gets that event; reconnect must downgrade it so the new runtime's turn can retire its dead prompt.
  it('retires a prompt whose runtime was only turn-live when the backend restarted', () => {
    recordSessionEventScope({ ...HUB, session_id: 'rt-old' })
    publishSessionState('rt-old', state({ needsInput: true, turnLive: true }))

    reconcileBusyStatesOnReconnect(HUB_SCOPE)
    expect($sessionStates.get()['rt-old']).toMatchObject({ needsInput: true, turnLive: false })

    recordSessionEventScope({ ...HUB, session_id: 'rt-new' })
    publishSessionState('rt-new', state({ busy: true, turnLive: true }))
    publishSessionState('rt-new', state({ busy: false }))

    expect($sessionStates.get()['rt-old']?.needsInput).toBe(false)
    expect($attentionSessionIds.get()).toEqual([])
  })

  it('never pairs runtimes that have no stored session yet', () => {
    publishSessionState('rt-old', state({ needsInput: true, storedSessionId: null }))
    publishSessionState('rt-new', state({ busy: true, storedSessionId: null }))

    expect($sessionStates.get()['rt-old']?.needsInput).toBe(true)
  })

  it('a turnLive claim alone proves the old runtime dead', () => {
    const idle = state({})
    publishSessionState('rt-idle', idle)
    publishSessionState('rt-old', state({ needsInput: true }))
    publishSessionState('rt-new', state({ turnLive: true }))

    expect($sessionStates.get()['rt-old']?.needsInput).toBe(false)
    expect($sessionStates.get()['rt-idle']).toBe(idle) // a sibling with no prompt is not republished
  })
})
