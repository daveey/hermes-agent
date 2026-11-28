import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { OpenTimingRecord } from '@/lib/open-timing'
import { markOpenClick, resetOpenTimings } from '@/lib/open-timing'
import { $messages, $selectedStoredSessionId, setMessages } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import { clearTranscriptTails, saveTranscriptTail } from '@/store/transcript-tail-cache'
import type { SessionInfo } from '@/types/hermes'

import { prepaintClickedTranscript } from './provisional-transcript'

// [daveey] A click paints the cached transcript itself, so it lands with the route change instead of 80-100 ms later
// in the route effect's resume. Only when the click loads the session into main.

const owner = { connectionId: 'hermes-hub', profile: 'default' }
const row = (over: Partial<SessionInfo> = {}) =>
  ({ connection_id: 'hermes-hub', id: 'h1', profile: 'default', ...over }) as SessionInfo
const cached = [{ content: 'cached', id: 1, role: 'user', timestamp: 1 }]
const record = () => (globalThis as { __hermesOpenTimings?: OpenTimingRecord[] }).__hermesOpenTimings

beforeEach(() => {
  window.localStorage.clear()
  clearTranscriptTails()
  resetOpenTimings()
  setMessages([])
  $selectedStoredSessionId.set(null)
  $sessionTiles.set([])
  saveTranscriptTail('h1', cached as never, owner)
})

afterEach(() => {
  clearTranscriptTails()
  setMessages([])
})

describe('prepaintClickedTranscript', () => {
  it('puts the cached rows on screen and records the paint against the click', () => {
    markOpenClick('h1', 'hermes-hub')

    expect(prepaintClickedTranscript('h1', row())).toBe(true)
    expect(JSON.stringify($messages.get())).toContain('cached')
    expect(record()).toBeDefined()
  })

  it('leaves a session that is already selected or open as a tile alone (the click focuses it)', () => {
    $selectedStoredSessionId.set('h1')
    expect(prepaintClickedTranscript('h1', row())).toBe(false)

    $selectedStoredSessionId.set(null)
    $sessionTiles.set([{ storedSessionId: 'h1' } as never])
    expect(prepaintClickedTranscript('h1', row())).toBe(false)
    expect($messages.get()).toEqual([])
  })

  it('leaves an asleep row to its read-only open, and paints nothing without a cached copy', () => {
    expect(prepaintClickedTranscript('h1', row({ offline_cached: true }))).toBe(false)
    expect(prepaintClickedTranscript('h2', row({ id: 'h2' }))).toBe(false)
    expect($messages.get()).toEqual([])
  })
})
