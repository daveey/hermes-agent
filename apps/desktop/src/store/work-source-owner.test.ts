import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $gateway, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import {
  $activeSessionId,
  $cronSessions,
  $messagingSessions,
  $selectedStoredSessionId,
  $unlistedSessionOwnerRows,
  _resetSessionOwnerHintsForTests,
  forgetSessionOwnerHintsForSession,
  setSessionOwnerHint,
  setSessions
} from '@/store/session'
import {
  $sessionTiles,
  clearAllSessionStates,
  publishSessionState,
  recordSessionEventScope
} from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

import {
  $workSourceAuthorityRevision,
  _resetWorkSourceOwnerForTests,
  qualifiedWorkSourceOwner,
  workSourceAuthorityForSession
} from './work-source-owner'

function mockGateway() {
  const handlers = new Set<(state: string) => void>()

  return {
    connectionState: 'open',
    request: vi.fn(async () => ({})),
    onEvent: () => () => undefined,
    onState(handler: (state: string) => void) {
      handlers.add(handler)

      return () => handlers.delete(handler)
    },
    emit(state: string) {
      this.connectionState = state

      for (const handler of handlers) {
        handler(state)
      }
    }
  }
}

function positive(n: number | null): n is number {
  return n !== null && Number.isSafeInteger(n) && n > 0
}

describe('work-source owner authority context', () => {
  const ambient = vi.fn(async () => {
    throw new Error('ambient route must not be used')
  })

  let primary = mockGateway()

  beforeEach(() => {
    primary = mockGateway()
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
      connections: { list: async () => null },
      logLine: vi.fn()
    }
    $connectionsRegistry.set({
      activeConnectionId: 'local',
      primary: 'local',
      connections: [
        { id: 'local', kind: 'local', label: 'Local' },
        { id: 'homelab', kind: 'remote', label: 'Homelab' }
      ]
    } as never)
    $profiles.set([{ name: 'default' }] as never)
    ambient.mockClear()
    setPrimaryGateway(primary as never, 'default')
    setPrimaryGatewayConnection({ connectionId: 'local' })
    $gateway.set({ request: ambient } as never)
    publishSessionState('rt-main', createClientSessionState('stored-chat'))
  })

  afterEach(() => {
    _resetWorkSourceOwnerForTests()
    clearAllSessionStates()
    $sessionTiles.set([])
    setSessions([])
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $profiles.set([])
    $gateway.set(null as never)
    setPrimaryGateway(null)
    $connectionsRegistry.set(null)
    _resetSessionOwnerHintsForTests({ storage: true })
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('pins a bare primary profile to the published primary, never ambient', () => {
    const owner = qualifiedWorkSourceOwner('default')

    expect(owner).toEqual({ connectionId: 'local', profile: 'default', routeKey: 'primary:default' })
    expect(ambient).not.toHaveBeenCalled()
  })

  it('does not guess ambient when the primary route is unpublished', () => {
    setPrimaryGateway(null)
    setPrimaryGatewayConnection(null)
    expect(qualifiedWorkSourceOwner('default')).toBeUndefined()
    expect(qualifiedWorkSourceOwner({ connectionId: '  ', profile: 'default' })).toBeUndefined()
    expect(workSourceAuthorityForSession(undefined, 'default', 'stored-chat')).toEqual({
      durable: null,
      runtime: null
    })
    expect(ambient).not.toHaveBeenCalled()
  })

  it('does not treat a non-primary bare profile as an owner', () => {
    expect(qualifiedWorkSourceOwner('other')).toBeUndefined()
    expect(workSourceAuthorityForSession(undefined, 'other', 'stored-chat')).toEqual({
      durable: null,
      runtime: null
    })
  })

  it('keeps an exact connection route qualified even when it is not the primary', () => {
    const owner = qualifiedWorkSourceOwner({ connectionId: 'homelab', profile: 'default' })

    expect(owner?.connectionId).toBe('homelab')
    expect(owner?.profile).toBe('default')
    expect(owner?.routeKey).not.toBe('primary:default')
    expect(ambient).not.toHaveBeenCalled()
  })

  it.each(['local', undefined])('issues positive epochs for a real idle-row owner tagged %s', connectionId => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: connectionId })
    setSessions([row])
    const authority = workSourceAuthorityForSession(row.connection_id ?? undefined, row.profile, row.id)

    expect(positive(authority.durable)).toBe(true)
    expect(positive(authority.runtime)).toBe(true)
    const revision = $workSourceAuthorityRevision.get()
    expect(workSourceAuthorityForSession(row.connection_id ?? undefined, row.profile, row.id)).toEqual(authority)
    expect($workSourceAuthorityRevision.get()).toBe(revision)
    expect(ambient).not.toHaveBeenCalled()
  })

  it('does not certify a session on a different qualified owner', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    $sessionTiles.set([
      {
        storedSessionId: 'stored-chat',
        runtimeId: 'rt-main',
        ownerRoute: { connectionId: 'local', profile: 'default' }
      }
    ])

    expect(workSourceAuthorityForSession('homelab', 'default', 'stored-chat')).toEqual({
      durable: null,
      runtime: null
    })
  })

  it('invalidates runtime on tile rebind and both epochs on semantic owner change, not on the same owner', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-a', ownerRoute: { connectionId: 'local', profile: 'default' } }
    ])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    const revision = $workSourceAuthorityRevision.get()

    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-a', ownerRoute: { connectionId: 'local', profile: 'default' } }
    ])
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat')).toEqual(first)
    expect($workSourceAuthorityRevision.get()).toBe(revision)

    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-b', ownerRoute: { connectionId: 'local', profile: 'default' } }
    ])
    const rebound = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(rebound.durable).toBe(first.durable)
    expect(positive(rebound.runtime)).toBe(true)
    expect(rebound.runtime).not.toBe(first.runtime)

    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-b', ownerRoute: { connectionId: 'homelab', profile: 'default' } }
    ])
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat')).toEqual({
      durable: null,
      runtime: null
    })
    // There is no dialed homelab socket: owner metadata alone is not authority.
    expect(workSourceAuthorityForSession('homelab', 'default', 'stored-chat')).toEqual({ durable: null, runtime: null })
  })

  it('treats ABA owner A→B→A between renders as a new generation', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-a', ownerRoute: { connectionId: 'local', profile: 'default' } }
    ])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')

    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-a', ownerRoute: { connectionId: 'homelab', profile: 'default' } }
    ])
    $sessionTiles.set([
      { storedSessionId: 'stored-chat', runtimeId: 'rt-a', ownerRoute: { connectionId: 'local', profile: 'default' } }
    ])
    const again = workSourceAuthorityForSession('local', 'default', 'stored-chat')

    expect(again.durable).not.toBe(first.durable)
    expect(again.runtime).not.toBe(first.runtime)
    expect(positive(again.durable)).toBe(true)
    expect(positive(again.runtime)).toBe(true)
    expect($workSourceAuthorityRevision.get()).toBeGreaterThan(1)
  })

  it('invalidates runtime on supported primary onState, without a private generation', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')

    primary.emit('closed')
    const closed = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(closed).toEqual({ durable: null, runtime: null })

    primary.connectionState = 'open'
    primary.emit('open')
    const opened = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(opened.runtime).not.toBe(first.runtime)
    expect(opened.durable).not.toBe(first.durable)
  })

  it('uses the main selected/active ladder when no tile is bound', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    $selectedStoredSessionId.set('stored-chat')
    $activeSessionId.set('rt-main')
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')

    $activeSessionId.set('rt-next')
    const rebound = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(rebound.durable).toBe(first.durable)
    expect(rebound.runtime).not.toBe(first.runtime)
  })

  it('does not issue usable authority without a live socket', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    workSourceAuthorityForSession('local', 'default', 'stored-chat')
    primary.emit('closed')
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat')).toEqual({ durable: null, runtime: null })
  })

  it('observes row-only owner ABA before another read', () => {
    const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })
    setSessions([row])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    setSessions([{ ...row, connection_id: 'homelab' }])
    setSessions([row])
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat')).not.toEqual(first)
  })

  it('observes mirror-only runtime replacement', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    publishSessionState('rt-next', createClientSessionState('stored-chat'))
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat').runtime).not.toBe(first.runtime)
  })

  it('does not certify an unknown owner merely because the caller supplies a live route', () => {
    expect(workSourceAuthorityForSession('local', 'default', 'missing')).toEqual({ durable: null, runtime: null })
  })

  it('does not reuse epochs after observer reset', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    _resetWorkSourceOwnerForTests()
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat').durable).not.toBe(first.durable)
  })

  it('keeps stream-only updates neutral', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    const revision = $workSourceAuthorityRevision.get()
    publishSessionState('rt-main', { ...createClientSessionState('stored-chat'), streaming: true } as never)
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat')).toEqual(first)
    expect($workSourceAuthorityRevision.get()).toBe(revision)
  })

  it('observes hint-only owner ABA synchronously', () => {
    const owner = { connectionId: 'local', profile: 'default' }
    setSessionOwnerHint('stored-chat', owner)
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(positive(first.durable)).toBe(true)
    forgetSessionOwnerHintsForSession('stored-chat')
    setSessionOwnerHint('stored-chat', owner)
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat').durable).not.toBe(first.durable)
  })

  it('observes runtime event-owner ABA without a mirror publication', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default' })])
    recordSessionEventScope({ session_id: 'rt-main', connectionId: 'local', profile: 'default' })
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    recordSessionEventScope({ session_id: 'rt-main', connectionId: 'homelab', profile: 'default' })
    recordSessionEventScope({ session_id: 'rt-main', connectionId: 'local', profile: 'default' })
    const again = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(again.runtime).not.toBe(first.runtime)
    expect(again.durable).toBe(first.durable)
  })

  it.each([$cronSessions, $messagingSessions, $unlistedSessionOwnerRows])(
    'observes owner changes from every non-recents row source',
    store => {
      const row = makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })

      try {
        store.set([row])
        const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
        expect(positive(first.durable)).toBe(true)
        store.set([{ ...row, connection_id: 'homelab' }])
        store.set([row])
        expect(workSourceAuthorityForSession('local', 'default', 'stored-chat').durable).not.toBe(first.durable)
      } finally {
        store.set([])
      }
    }
  )

  it('does not reuse authority after LRU eviction', () => {
    const rows = Array.from({ length: 4100 }, (_, i) =>
      makeSessionInfo({ id: `chat-${i}`, profile: 'default', connection_id: 'local' })
    )

    setSessions(rows)
    const first = workSourceAuthorityForSession('local', 'default', rows[0].id)

    for (const row of rows.slice(1)) {
      workSourceAuthorityForSession('local', 'default', row.id)
    }
    expect(workSourceAuthorityForSession('local', 'default', rows[0].id).durable).not.toBe(first.durable)
  })

  it('invalidates when the published primary socket identity is replaced', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    const next = mockGateway()
    setPrimaryGateway(next as never, 'default')
    setPrimaryGatewayConnection({ connectionId: 'local' })
    const swapped = workSourceAuthorityForSession('local', 'default', 'stored-chat')

    expect(positive(swapped.runtime)).toBe(true)
    expect(swapped.runtime).not.toBe(first.runtime)
  })
})
