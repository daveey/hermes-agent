import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { onGatewayEvent } from '@/contrib/events'

import {
  bindUiLatency,
  cancelUiLatency,
  completeUiLatency,
  finishUiLatencyAfterPaint,
  peekUiLatency,
  resetUiLatencyForTests,
  startUiLatency,
  UI_LATENCY_MAX_MS
} from './ui-latency'

type Payload = Record<string, unknown>

let frames: Array<() => void> = []
let events: Payload[] = []

let off = () => {}
let mono = 0

/** Run every queued animation-frame callback once; callbacks queued meanwhile wait for the next frame. */
const frame = () => {
  const run = frames
  frames = []
  run.forEach(callback => callback())
}

const setVisibility = (state: 'hidden' | 'visible') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
}

beforeEach(() => {
  frames = []
  events = []
  mono = 1000
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
  vi.setSystemTime(1_700_000_000_000)
  vi.spyOn(performance, 'now').mockImplementation(() => mono)
  vi.stubGlobal('requestAnimationFrame', (callback: () => void) => frames.push(callback))
  setVisibility('visible')
  off = onGatewayEvent('desktop.ui_latency', event => events.push({ ...(event.payload as Payload) }))
})

afterEach(() => {
  off()
  resetUiLatencyForTests()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  setVisibility('visible')
})

describe('ui latency producer', () => {
  it('emits only after the SECOND frame past the commit, with identifiers only', () => {
    startUiLatency('ui.session_switch', { gateway: 'mac-mini', profile: 'p', session_id: 's1' })
    mono += 120
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 's1' })

    expect(events).toEqual([])
    frame()
    expect(events).toEqual([]) // first frame: the commit is only being painted
    mono += 16
    frame()

    expect(events).toEqual([
      {
        duration_ms: 136,
        gateway: 'mac-mini',
        name: 'ui.session_switch',
        profile: 'p',
        session_id: 's1',
        start_ms: 1_700_000_000_000
      }
    ])
  })

  it('drops empty ids and never carries a field outside the contract', () => {
    startUiLatency('ui.card_read', { board: 'ops', card_id: 't_1', gateway: '', profile: undefined })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 't_1' })
    frame()
    frame()

    expect(Object.keys(events[0]).sort()).toEqual(['board', 'card_id', 'duration_ms', 'name', 'start_ms'])
  })

  it('takes the duration from the monotonic clock: a wall-clock step cannot make it negative', () => {
    startUiLatency('ui.new_session', {}, { runtimeId: null })
    vi.setSystemTime(1_699_999_000_000) // clock stepped back an hour mid-span
    mono += 40
    finishUiLatencyAfterPaint('ui.new_session')
    frame()
    frame()

    expect(events[0]).toMatchObject({ duration_ms: 40, name: 'ui.new_session', start_ms: 1_700_000_000_000 })
  })

  it('leaves the span pending when the committed result is for another session or card', () => {
    startUiLatency('ui.session_switch', { session_id: 'wanted' })
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 'other' })
    frame()
    frame()
    expect(events).toEqual([])
    expect(peekUiLatency('ui.session_switch')).not.toBeNull()

    startUiLatency('ui.card_read', { card_id: 'a' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'b' })
    frame()
    frame()
    expect(events).toEqual([])
  })

  it('a superseding click cancels the first span, and session/new-session share one navigation channel', () => {
    startUiLatency('ui.session_switch', { session_id: 'a' })
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 'a' })
    frame() // first frame done, second still queued
    startUiLatency('ui.session_switch', { session_id: 'b' })
    frame()
    expect(events).toEqual([]) // 'a' was superseded before its paint

    startUiLatency('ui.new_session', {}, { runtimeId: null }) // supersedes the pending switch to 'b'
    expect(peekUiLatency('ui.session_switch')).toBeNull()
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 'b' })
    frame()
    frame()
    expect(events).toEqual([])
  })

  it('cards and sessions do not supersede each other', () => {
    startUiLatency('ui.session_switch', { session_id: 's' })
    startUiLatency('ui.card_read', { card_id: 'c' })
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 's' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' })
    frame()
    frame()

    expect(events.map(event => event.name).sort()).toEqual(['ui.card_read', 'ui.session_switch'])
  })

  it('a failure cancels: nothing is emitted even if a finisher fires afterwards', () => {
    startUiLatency('ui.card_read', { card_id: 'c' })
    cancelUiLatency('ui.card_read')
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' })
    frame()
    frame()

    expect(events).toEqual([])
  })

  it('cancels when the result is gone again after the frames (stillValid false, or it throws)', () => {
    startUiLatency('ui.card_read', { card_id: 'c' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' }, () => false)
    frame()
    frame()

    startUiLatency('ui.card_read', { card_id: 'c' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' }, () => {
      throw new Error('gone')
    })
    frame()
    frame()

    expect(events).toEqual([])
    expect(peekUiLatency('ui.card_read')).toBeNull()
  })

  it('cancels every pending span when the window is hidden, and when it is hidden at the second frame', () => {
    startUiLatency('ui.session_switch', { session_id: 's' })
    startUiLatency('ui.card_read', { card_id: 'c' })
    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    setVisibility('visible')
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 's' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' })
    frame()
    frame()
    expect(events).toEqual([])

    startUiLatency('ui.card_read', { card_id: 'c' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' })
    frame()
    setVisibility('hidden')
    frame()
    expect(events).toEqual([])
  })

  it('never measures without a frame clock', () => {
    vi.stubGlobal('requestAnimationFrame', undefined)
    startUiLatency('ui.card_read', { card_id: 'c' })
    finishUiLatencyAfterPaint('ui.card_read', { card_id: 'c' })

    expect(peekUiLatency('ui.card_read')).toBeNull()
    expect(events).toEqual([])
  })

  it('drops a span whose result never shows once the time bound passes', () => {
    startUiLatency('ui.session_switch', { session_id: 's' })
    vi.advanceTimersByTime(UI_LATENCY_MAX_MS + 1)
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 's' })
    frame()
    frame()

    expect(events).toEqual([])
  })

  it('completes a create at once, with the new id, and only when a span is pending', () => {
    completeUiLatency('ui.card_create', { card_id: 'orphan' })
    expect(events).toEqual([])

    startUiLatency('ui.card_create', { board: 'ops', gateway: 'local' })
    mono += 75
    completeUiLatency('ui.card_create', { card_id: 't_new' })

    expect(events).toEqual([
      {
        board: 'ops',
        card_id: 't_new',
        duration_ms: 75,
        gateway: 'local',
        name: 'ui.card_create',
        start_ms: 1_700_000_000_000
      }
    ])

    completeUiLatency('ui.card_create', { card_id: 't_new' }) // a span reports once
    expect(events).toHaveLength(1)
  })

  it('bind adds the facts learned after the click', () => {
    startUiLatency('ui.new_session', {}, { runtimeId: '\u0000unbound' })
    bindUiLatency('ui.new_session', { session_id: 'stored-1' }, { runtimeId: 'rt-1' })

    expect(peekUiLatency('ui.new_session')).toEqual({ expect: { runtimeId: 'rt-1' }, ids: { session_id: 'stored-1' } })
  })

  it('a stale attempt token cannot bind, cancel or complete a newer attempt', () => {
    const first = startUiLatency('ui.card_create', { board: 'a' })
    const second = startUiLatency('ui.card_create', { board: 'b' })

    expect(second).not.toBe(first)
    bindUiLatency('ui.card_create', { card_id: 'old' }, {}, first)
    cancelUiLatency('ui.card_create', first)
    completeUiLatency('ui.card_create', { card_id: 'old' }, first)

    expect(events).toEqual([])
    expect(peekUiLatency('ui.card_create')?.ids).toEqual({ board: 'b' })

    completeUiLatency('ui.card_create', { card_id: 'new' }, second)
    expect(events).toMatchObject([{ board: 'b', card_id: 'new' }])
  })

  it('a token for the current attempt cancels it', () => {
    const token = startUiLatency('ui.new_session', {}, { runtimeId: null })
    cancelUiLatency('ui.new_session', token)

    expect(peekUiLatency('ui.new_session')).toBeNull()
  })

  it('a finisher that already captured its span does not emit for a newer start', () => {
    startUiLatency('ui.session_switch', { session_id: 's1' })
    finishUiLatencyAfterPaint('ui.session_switch', { session_id: 's1' })
    startUiLatency('ui.session_switch', { session_id: 's2' })
    frame()
    frame()

    expect(events).toEqual([])
    expect(peekUiLatency('ui.session_switch')?.ids).toEqual({ session_id: 's2' })
  })
})
