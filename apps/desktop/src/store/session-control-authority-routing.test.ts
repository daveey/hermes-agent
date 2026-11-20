import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { useWorkSourceUnavailable } from '@/lib/use-work-source-unavailable'
import { sessionsHost } from '@/sdk/sessions'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $gateway, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import { _resetSessionOwnerHintsForTests, setSessions } from '@/store/session'
import { $sessionControlBySession, clearAllSessionControl, refreshSessionControl } from '@/store/session-control'
import { sessionStatusStripe } from '@/store/session-dot-state'
import { $sessionTiles, clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { $unavailableWorkSources } from '@/store/session-work-source'
import { _resetWorkSourceOwnerForTests } from '@/store/work-source-owner'
import { makeSessionInfo } from '@/test/session-info'

const CONTROL = { goal: null, heartbeat: null, loop: null, revision: 'r1', updated_at: 1 }
const DONE = { kind: 'done', source: 'derived', at: 1000 } as const

function stripe(unavailable: boolean) {
  return sessionStatusStripe('idle', true, false, 0, false, null, 0, 0, 0, DONE, unavailable)
}

describe('control authority at the real idle row hook', () => {
  const request = vi.fn(async () => ({ control: CONTROL }))

  const ambient = vi.fn(async () => {
    throw new Error('ambient route must not be used')
  })

  beforeEach(() => {
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
      connections: { list: async () => null },
      logLine: vi.fn()
    }
    $connectionsRegistry.set({
      activeConnectionId: 'local',
      primary: 'local',
      connections: [{ id: 'local', kind: 'local', label: 'Local' }]
    } as never)
    $profiles.set([{ name: 'default' }] as never)
    request.mockReset().mockResolvedValue({ control: CONTROL })
    ambient.mockClear()
    setPrimaryGateway(
      { onEvent: () => () => undefined, onState: () => () => undefined, request, connectionState: 'open' } as never,
      'default'
    )
    setPrimaryGatewayConnection({ connectionId: 'local' })
    $gateway.set({ request: ambient } as never)
    publishSessionState('rt-main', createClientSessionState('stored-chat'))
  })

  afterEach(() => {
    _resetWorkSourceOwnerForTests()
    clearAllSessionControl()
    clearAllSessionStates()
    $sessionTiles.set([])
    setSessions([])
    $unavailableWorkSources.set({})
    $profiles.set([])
    $gateway.set(null as never)
    setPrimaryGateway(null)
    $connectionsRegistry.set(null)
    _resetSessionOwnerHintsForTests({ storage: true })
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it.each(['local', undefined])('control timeout vetoes stored done with connection tag %s', async connectionId => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: connectionId, outcome: DONE })
    setSessions([row])
    await refreshSessionControl('rt-main')

    for (const source of ['lineage', 'tasks', 'process', 'subagent'] as const) {
      const token = sessionsHost.beginWorkSourceRead({
        connectionId: 'local',
        profile: 'default',
        storedSessionId: row.id,
        source
      })

      sessionsHost.reportWorkSourceRead({ ...token, available: true })
    }

    const { result, unmount } = renderHook(() => useWorkSourceUnavailable(row))

    try {
      expect(stripe(result.current)).toBe('done')
      request.mockRejectedValueOnce(new Error('session.control.read timed out'))
      await act(async () => {
        await refreshSessionControl('rt-main')
      })
      expect(request).toHaveBeenCalledWith('session.control.read', { session_id: 'rt-main' })
      expect(ambient).not.toHaveBeenCalled()
      expect($sessionControlBySession.get()['rt-main']?.error).toContain('timed out')
      expect(stripe(result.current)).not.toBe('done')
      await act(async () => {
        await refreshSessionControl('rt-main')
      })
      expect(stripe(result.current)).toBe('done')
    } finally {
      unmount()
    }
  })

  it('does not reuse a request token after reset and accept the previous connection reply', async () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    let resolveOld!: (value: { control: typeof CONTROL }) => void
    let resolveNew!: (value: { control: typeof CONTROL }) => void
    request.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveOld = resolve
        })
    )
    const oldRead = refreshSessionControl('rt-main')
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    clearAllSessionControl()
    request.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveNew = resolve
        })
    )
    const newRead = refreshSessionControl('rt-main')
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    resolveOld({ control: { ...CONTROL, revision: 'old-connection' } })
    await oldRead
    const snapshotAfterOldReply = $sessionControlBySession.get()['rt-main']?.snapshot
    resolveNew({ control: { ...CONTROL, revision: 'new-connection' } })
    await newRead
    expect(snapshotAfterOldReply).toBeNull()
    expect($sessionControlBySession.get()['rt-main']?.snapshot?.revision).toBe('new-connection')
  })

  it('revokes completed reads on primary replacement without waiting for another poll', async () => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local', outcome: DONE })
    setSessions([row])
    await refreshSessionControl('rt-main')

    const reads = (['lineage', 'tasks', 'process', 'subagent'] as const).map(source =>
      sessionsHost.beginWorkSourceRead({ connectionId: 'local', profile: 'default', storedSessionId: row.id, source })
    )

    for (const read of reads) {
      sessionsHost.reportWorkSourceRead({ ...read, available: true })
    }
    const { result, unmount } = renderHook(() => useWorkSourceUnavailable(row))

    try {
      expect(stripe(result.current)).toBe('done')
      act(() => {
        setPrimaryGateway(
          { onEvent: () => () => undefined, onState: () => () => undefined, request, connectionState: 'open' } as never,
          'default'
        )
      })
      expect(stripe(result.current)).not.toBe('done')
      act(() => {
        for (const read of reads) {
          sessionsHost.reportWorkSourceRead({ ...read, available: true })
        }
      })
      expect(stripe(result.current)).not.toBe('done')
    } finally {
      unmount()
    }
  })

  it('rejects a successful old control reply across owner ABA', async () => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })
    setSessions([row])
    let resolve!: (value: { control: typeof CONTROL }) => void
    request.mockImplementationOnce(
      () =>
        new Promise(done => {
          resolve = done
        })
    )
    const pending = refreshSessionControl('rt-main')
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    setSessions([{ ...row, connection_id: 'elsewhere' }])
    setSessions([row])
    resolve({ control: CONTROL })
    await pending
    expect($sessionControlBySession.get()['rt-main']?.snapshot).toBeNull()
    expect(Object.values($unavailableWorkSources.get()).every(read => !read.available)).toBe(true)
  })

  it('rejects pending SDK runtime proof after a mirror binding changes but preserves durable proofs', () => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local', outcome: DONE })
    setSessions([row])

    const reads = (['lineage', 'tasks', 'process', 'subagent', 'control'] as const).map(source =>
      sessionsHost.beginWorkSourceRead({ connectionId: 'local', profile: 'default', storedSessionId: row.id, source })
    )

    publishSessionState('rt-next', createClientSessionState('stored-chat'))

    for (const read of reads) {
      sessionsHost.reportWorkSourceRead({ ...read, available: true })
    }
    const { result, unmount } = renderHook(() => useWorkSourceUnavailable(row))

    try {
      expect(stripe(result.current)).not.toBe('done')
      act(() => {
        for (const source of ['process', 'subagent', 'control'] as const) {
          const token = sessionsHost.beginWorkSourceRead({
            connectionId: 'local',
            profile: 'default',
            storedSessionId: row.id,
            source
          })

          sessionsHost.reportWorkSourceRead({ ...token, available: true })
        }
      })
      expect(stripe(result.current)).toBe('done')
    } finally {
      unmount()
    }
  })

  it('unread required sources cannot certify stored done before the first poll', () => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local', outcome: DONE })
    setSessions([row])
    const { result, unmount } = renderHook(() => useWorkSourceUnavailable(row))

    try {
      expect(stripe(result.current)).not.toBe('done')
    } finally {
      unmount()
    }
  })
})
