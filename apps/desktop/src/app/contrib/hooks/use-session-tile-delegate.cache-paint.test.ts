import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import type * as HermesModule from '@/hermes'
import { chatMessageText } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { resetOpenTimings } from '@/lib/open-timing'
import { setSessionOwnerHint, setSessions } from '@/store/session'
import { $sessionTiles, sessionTileDelegate } from '@/store/session-states'
import { clearTranscriptTails, readCachedTranscript, saveTranscriptTail } from '@/store/transcript-tail-cache'
import type { SessionMessage } from '@/types/hermes'

import { clearSingleFlightSessionResumeState } from '../../session/hooks/use-prompt-actions/single-flight-resume'

import { useSessionTileDelegate } from './use-session-tile-delegate'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: '' })),
  getSession: vi.fn()
}))
vi.mock('@/store/gateway', async importActual => ({
  ...(await importActual<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn()
}))

const { getLatestSessionMessages } = await import('@/hermes')
const { requestGatewayForAgent } = await import('@/store/gateway')

const ROUTE = { connectionId: 'hermes-hub', mode: 'remote' as const, profile: 'default', targetProfile: 'default' }
const SCOPE = { connectionId: 'hermes-hub', profile: 'default' }

const rows = (...texts: string[]): SessionMessage[] =>
  texts.map((text, index) => ({ content: text, id: index + 1, role: index % 2 ? 'assistant' : 'user' }) as never)

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => (resolve = done))

  return { promise, resolve }
}

function renderTile() {
  const states = new Map<string, ClientSessionState>()

  const updateSessionState = vi.fn(
    (runtimeId: string, updater: (state: ClientSessionState) => ClientSessionState, storedSessionId?: string) => {
      states.set(runtimeId, updater(states.get(runtimeId) ?? createClientSessionState(storedSessionId ?? null)))
    }
  )

  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchLoadedSession: vi.fn(async () => false) as never,
      branchStoredSession: vi.fn(async () => undefined),
      executeSlashCommand: vi.fn(async () => undefined) as never,
      removeSession: vi.fn(async () => undefined),
      requestGateway: vi.fn(async () => ({})) as never,
      runtimeIdByStoredSessionIdRef: { current: new Map() } as never,
      sessionStateByRuntimeIdRef: { current: new Map() } as never,
      updateSessionState: updateSessionState as never
    })
  )

  return { states }
}

const texts = (state?: ClientSessionState) => state?.messages.map(message => chatMessageText(message)) ?? []

const tileLines = () =>
  ((globalThis as { __hermesOpenTimings?: Record<string, unknown>[] }).__hermesOpenTimings ?? []).filter(
    record => record.surface === 'tile'
  )

describe('[daveey] a tile (the @session ref click) paints from the transcript cache', () => {
  beforeEach(() => {
    clearSingleFlightSessionResumeState()
    clearTranscriptTails()
    resetOpenTimings()
    setSessions([])
    $sessionTiles.set([])
    vi.mocked(getLatestSessionMessages).mockReset()
    vi.mocked(requestGatewayForAgent).mockReset()
  })

  afterEach(() => {
    clearSingleFlightSessionResumeState()
    clearTranscriptTails()
    setSessions([])
    $sessionTiles.set([])
  })

  it('binds with the cached rows while REST is in flight, then reconciles to the REST page', async () => {
    setSessionOwnerHint('old-ref', ROUTE)
    saveTranscriptTail('old-ref', rows('cached question', 'cached answer'), SCOPE)
    const rest = deferred<{ messages: SessionMessage[]; session_id: string }>()
    vi.mocked(getLatestSessionMessages).mockReturnValue(rest.promise as never)
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'runtime-old' } as never)
    const { states } = renderTile()

    // Resolves without the REST page: the tile is not held on hub REST.
    await expect(sessionTileDelegate()!.resumeTile('old-ref')).resolves.toBe('runtime-old')
    expect(getLatestSessionMessages).toHaveBeenCalledWith('old-ref', SCOPE)
    expect(texts(states.get('runtime-old'))).toEqual(['cached question', 'cached answer'])

    rest.resolve({ messages: rows('cached question', 'cached answer', 'newer question'), session_id: 'old-ref' })
    await vi.waitFor(() =>
      expect(texts(states.get('runtime-old'))).toEqual(['cached question', 'cached answer', 'newer question'])
    )

    // The page refreshed the cache, and the open wrote one tile line painted from cache.
    expect(readCachedTranscript('old-ref', SCOPE)?.messages).toHaveLength(3)
    expect(tileLines()).toEqual([
      expect.objectContaining({ changed: true, id: 'old-ref', outcome: 'rest', paint: 'cache', paintRows: 2 })
    ])
  })

  it('without a cached copy it waits on REST exactly as before and logs paint "rest"', async () => {
    setSessionOwnerHint('cold-ref', ROUTE)
    vi.mocked(getLatestSessionMessages).mockResolvedValue({
      messages: rows('from rest'),
      session_id: 'cold-ref'
    } as never)
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'runtime-cold' } as never)
    const { states } = renderTile()

    await expect(sessionTileDelegate()!.resumeTile('cold-ref')).resolves.toBe('runtime-cold')

    expect(texts(states.get('runtime-cold'))).toEqual(['from rest'])
    expect(tileLines()).toEqual([expect.objectContaining({ id: 'cold-ref', outcome: 'rest', paint: 'rest' })])
  })

  it('a cached copy under another owner key is not painted', async () => {
    setSessionOwnerHint('other-ref', ROUTE)
    saveTranscriptTail('other-ref', rows('wrong gateway'), { connectionId: 'mac-mini', profile: 'default' })
    vi.mocked(getLatestSessionMessages).mockResolvedValue({ messages: rows('right'), session_id: 'other-ref' } as never)
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'runtime-other' } as never)
    const { states } = renderTile()

    await sessionTileDelegate()!.resumeTile('other-ref')

    expect(texts(states.get('runtime-other'))).toEqual(['right'])
  })
})
