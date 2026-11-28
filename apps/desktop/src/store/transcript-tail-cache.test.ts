import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LATEST_SESSION_MESSAGES_LIMIT } from '@/api/sessions'
import type { SessionMessage } from '@/hermes'

import {
  clearTranscriptTails,
  dropTranscriptTail,
  dropTranscriptTailEverywhere,
  loadTranscriptTail,
  migrateTranscriptTailsForProfile,
  readCachedTranscript,
  saveOlderTranscriptPage,
  saveTranscriptTail
} from './transcript-tail-cache'

const row = (id: number, role: 'assistant' | 'user' = 'user'): SessionMessage =>
  ({ content: `text ${id}`, id, role, timestamp: 1_000 + id }) as SessionMessage

const rows = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, index) => row(from + index))
const MINI = { connectionId: 'mac-mini', profile: 'default' }

beforeEach(() => clearTranscriptTails())
afterEach(() => {
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
  clearTranscriptTails()
})

describe('transcript cache (renderer side)', () => {
  it('stores raw rows and paints them converted, scoped by connection and profile', () => {
    saveTranscriptTail('s1', [row(1), row(2, 'assistant')], MINI)

    const painted = loadTranscriptTail('s1', MINI)

    expect(painted?.map(message => message.role)).toEqual(['user', 'assistant'])
    expect(JSON.stringify(painted)).toContain('text 2')
    expect(loadTranscriptTail('s1', { connectionId: 'hermes-hub', profile: 'default' })).toBeNull()
    expect(loadTranscriptTail('s1', { connectionId: 'mac-mini', profile: 'alpha' })).toBeNull()
  })

  it('never reads without an owner scope', () => {
    saveTranscriptTail('s1', [row(1)], MINI)
    saveTranscriptTail('s2', [row(1)])

    expect(loadTranscriptTail('s1')).toBeNull()
    expect(readCachedTranscript('s2', MINI)).toBeNull()
  })

  it('paints the REST page size by default and every cached row on request', () => {
    saveTranscriptTail('s1', rows(1, 200), MINI)

    expect(loadTranscriptTail('s1', MINI)).toHaveLength(LATEST_SESSION_MESSAGES_LIMIT)
    expect(loadTranscriptTail('s1', MINI, { all: true })).toHaveLength(200)
  })

  it('prepends loaded older pages and drops by scope, everywhere, and on clear', () => {
    saveTranscriptTail('s1', rows(11, 20), MINI)
    saveOlderTranscriptPage('s1', rows(1, 10), MINI)
    expect(readCachedTranscript('s1', MINI)?.messages).toHaveLength(20)

    saveTranscriptTail('s1', rows(1, 2), { connectionId: 'hermes-hub', profile: 'default' })
    dropTranscriptTail('s1', MINI)
    expect(readCachedTranscript('s1', MINI)).toBeNull()
    expect(readCachedTranscript('s1', { connectionId: 'hermes-hub', profile: 'default' })).not.toBeNull()

    dropTranscriptTailEverywhere('s1')
    expect(readCachedTranscript('s1', { connectionId: 'hermes-hub', profile: 'default' })).toBeNull()
  })

  it('re-keys the local connection on a profile rename only', () => {
    saveTranscriptTail('s1', [row(1)], { connectionId: 'local', profile: 'old' })
    saveTranscriptTail('s2', [row(1)], { connectionId: 'mac-mini', profile: 'old' })

    migrateTranscriptTailsForProfile('old', 'new')

    expect(readCachedTranscript('s1', { connectionId: 'local', profile: 'new' })).not.toBeNull()
    expect(readCachedTranscript('s2', { connectionId: 'mac-mini', profile: 'old' })).not.toBeNull()
  })

  it('goes through the Electron bridge when one is present', () => {
    const bridge = {
      clear: vi.fn(),
      drop: vi.fn(),
      dropEverywhere: vi.fn(),
      migrateProfile: vi.fn(),
      put: vi.fn(),
      read: vi.fn(() => ({ activity: 1, messageCount: 1, messages: [row(7)], savedAt: 1 })),
      stats: vi.fn()
    }

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { transcriptCache: bridge }

    saveTranscriptTail('s1', [row(7)], 'work')
    saveOlderTranscriptPage('s1', [row(6)], MINI)

    expect(bridge.put).toHaveBeenNthCalledWith(
      1,
      { connectionId: '', profile: 'work', sessionId: 's1' },
      [row(7)],
      'latest'
    )
    expect(bridge.put).toHaveBeenNthCalledWith(2, { ...MINI, sessionId: 's1' }, [row(6)], 'older')
    expect(JSON.stringify(loadTranscriptTail('s1', MINI))).toContain('text 7')
    expect(bridge.read).toHaveBeenCalledWith({ ...MINI, sessionId: 's1' })
  })

  it('a throwing bridge degrades to "no cached paint", never an error', () => {
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      transcriptCache: {
        put: () => {
          throw new Error('ipc gone')
        },
        read: () => {
          throw new Error('ipc gone')
        }
      }
    }

    expect(() => saveTranscriptTail('s1', [row(1)], MINI)).not.toThrow()
    expect(loadTranscriptTail('s1', MINI)).toBeNull()
  })
})
