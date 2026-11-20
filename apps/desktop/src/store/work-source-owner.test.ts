import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $gateway, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import { $projectTree } from '@/store/project-tree'
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
  _ownerIndexBuildsForTests,
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
    $projectTree.set([])
    $unlistedSessionOwnerRows.set([])
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

  it('review: tile rebind revokes authority when both mirrors already exist', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    publishSessionState('rt-a', createClientSessionState('stored-chat'))
    publishSessionState('rt-b', createClientSessionState('stored-chat'))
    $sessionTiles.set([{ storedSessionId: 'stored-chat', runtimeId: 'rt-a', ownerRoute: { connectionId: 'local', profile: 'default' } }])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    $sessionTiles.set([{ storedSessionId: 'stored-chat', runtimeId: 'rt-b', ownerRoute: { connectionId: 'local', profile: 'default' } }])
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat').runtime).not.toBe(first.runtime)
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

  it('revokes same-socket primary route-tag ABA without a read in the intermediate route', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(positive(first.durable)).toBe(true)
    setPrimaryGatewayConnection({ connectionId: 'homelab' })
    setPrimaryGatewayConnection({ connectionId: 'local' })
    const after = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(positive(after.durable)).toBe(true)
    expect(after.durable).not.toBe(first.durable)
    expect(after.runtime).not.toBe(first.runtime)
  })

  it.each(['preview', 'repo'])('observes project-tree-only %s rows and isolates connection/profile scopes', kind => {
    const row = makeSessionInfo({ id: 'tree-chat', profile: 'default', connection_id: 'local' })
    const stable = makeSessionInfo({ id: 'stable-chat', profile: 'default', connection_id: 'local' })
    setSessions([stable])

    const publish = (connectionId: string, profile = 'default') => {
      const rows = [{ ...row, connection_id: connectionId, profile }]
      $projectTree.set([
        {
          id: 'project',
          label: 'Project',
          path: '/project',
          sessionCount: 1,
          previewSessions: kind === 'preview' ? rows : [],
          repos: kind === 'repo' ? [{ groups: [{ sessions: rows }] }] : []
        }
      ] as never)
    }

    publish('local')
    const first = workSourceAuthorityForSession('local', 'default', row.id)
    const unchanged = workSourceAuthorityForSession('local', 'default', stable.id)
    expect(positive(first.durable)).toBe(true)
    expect(workSourceAuthorityForSession('homelab', 'default', row.id)).toEqual({ durable: null, runtime: null })
    expect(workSourceAuthorityForSession('local', 'other', row.id)).toEqual({ durable: null, runtime: null })
    publish('homelab')
    publish('local', 'other')
    publish('local')
    const after = workSourceAuthorityForSession('local', 'default', row.id)
    expect(positive(after.durable)).toBe(true)
    expect(after.durable).not.toBe(first.durable)
    expect(workSourceAuthorityForSession('local', 'default', stable.id)).toEqual(unchanged)
  })

  it('preserves first-row lineage precedence when indexing owner observations', () => {
    const first = makeSessionInfo({
      id: 'tip',
      _lineage_root_id: 'root',
      _lineage_ids: ['middle', 'tip'],
      profile: 'default',
      connection_id: 'local'
    })

    const collision = makeSessionInfo({ id: 'root', profile: 'default', connection_id: 'homelab' })
    setSessions([first, collision])
    const root = workSourceAuthorityForSession('local', 'default', 'root')
    const middle = workSourceAuthorityForSession('local', 'default', 'middle')
    expect(positive(root.durable)).toBe(true)
    expect(positive(middle.durable)).toBe(true)
    setSessions([collision, first])
    expect(workSourceAuthorityForSession('local', 'default', 'root')).toEqual({ durable: null, runtime: null })
    expect(workSourceAuthorityForSession('local', 'default', 'middle')).toEqual(middle)
    setSessions([])
    expect(workSourceAuthorityForSession('local', 'default', 'middle')).toEqual({ durable: null, runtime: null })
    setSessions([first])
    expect(workSourceAuthorityForSession('local', 'default', 'middle').durable).not.toBe(middle.durable)
  })

  it('keeps same-ID rows on different routes independent while exact ids still outrank lineage aliases', () => {
    const local = makeSessionInfo({ id: 'twin', profile: 'default', connection_id: 'local' })
    const away = makeSessionInfo({ id: 'twin', profile: 'default', connection_id: 'homelab' })
    setSessions([away, local])
    const a = workSourceAuthorityForSession('local', 'default', 'twin')
    const b = workSourceAuthorityForSession('homelab', 'default', 'twin')
    expect(positive(a.durable)).toBe(true)
    expect(positive(b.durable)).toBe(false) // homelab has no live socket here; local must not be blocked by it
    setSessions([local, away])
    expect(workSourceAuthorityForSession('local', 'default', 'twin')).toEqual(a)
    // A third route never named by any row is still refused.
    expect(workSourceAuthorityForSession('elsewhere', 'default', 'twin')).toEqual({ durable: null, runtime: null })
    // An exact-id row of another route still outranks a same-scope lineage alias (list precedence kept).
    const alias = makeSessionInfo({ id: 'tip', _lineage_root_id: 'twin', profile: 'default', connection_id: 'local' })
    setSessions([away, alias])
    expect(workSourceAuthorityForSession('local', 'default', 'twin')).toEqual({ durable: null, runtime: null })
  })

  it('revokes only the runtime epoch when a tile or the main pane moves between mirrored runtimes, ABA included', () => {
    setSessions([makeSessionInfo({ id: 'stored-chat', profile: 'default', connection_id: 'local' })])
    publishSessionState('rt-a', createClientSessionState('stored-chat'))
    publishSessionState('rt-b', createClientSessionState('stored-chat'))
    const route = { connectionId: 'local', profile: 'default' }
    const tile = (runtimeId: string) => $sessionTiles.set([{ storedSessionId: 'stored-chat', runtimeId, ownerRoute: route }])
    tile('rt-a')
    const first = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    tile('rt-b')
    const second = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    tile('rt-a')
    const third = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(new Set([first.runtime, second.runtime, third.runtime]).size).toBe(3)
    expect([second.durable, third.durable]).toEqual([first.durable, first.durable])
    $sessionTiles.set([])
    const noTile = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    $activeSessionId.set('rt-a')
    const mainA = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    $activeSessionId.set('rt-b')
    const mainB = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    $activeSessionId.set('rt-a')
    const mainA2 = workSourceAuthorityForSession('local', 'default', 'stored-chat')
    expect(new Set([third.runtime, noTile.runtime, mainA.runtime, mainB.runtime, mainA2.runtime]).size).toBe(5)
    expect(mainA2.durable).toBe(first.durable)
    // Re-asserting the same effective roles is neutral.
    $activeSessionId.set('rt-a')
    expect(workSourceAuthorityForSession('local', 'default', 'stored-chat')).toEqual(mainA2)
  })

  it('builds the owner candidate index once per row-list change, even with an unlisted draft', () => {
    const rows = Array.from({ length: 200 }, (_, i) => makeSessionInfo({ id: `idx-${i}`, profile: 'default', connection_id: 'local' }))
    $unlistedSessionOwnerRows.set([makeSessionInfo({ id: 'draft', profile: 'default', connection_id: 'local' })])
    setSessions(rows)

    for (const row of rows) {
      publishSessionState(`rt-${row.id}`, createClientSessionState(row.id))
      workSourceAuthorityForSession('local', 'default', row.id)
    }

    const base = _ownerIndexBuildsForTests()

    // Repeated lookups and a stream publish with the same row list: no rebuild.
    for (const row of rows) {
      workSourceAuthorityForSession('local', 'default', row.id)
    }

    publishSessionState('rt-idx-0', { ...createClientSessionState('idx-0'), streaming: true } as never)
    expect(_ownerIndexBuildsForTests()).toBe(base)
    // One structural change rebuilds once, not once per tracked entry or runtime.
    setSessions(rows.map(row => ({ ...row, title: 'renamed' })))
    expect(_ownerIndexBuildsForTests()).toBeLessThanOrEqual(base + 1)
    expect(_ownerIndexBuildsForTests()).toBeGreaterThan(base)
    const after = _ownerIndexBuildsForTests()
    $unlistedSessionOwnerRows.set([makeSessionInfo({ id: 'draft-2', profile: 'default', connection_id: 'local' })])
    expect(_ownerIndexBuildsForTests()).toBeLessThanOrEqual(after + 1)
  })

  it.each([false, true])('review: realistic structural cost with unlisted stub %s', withStub => {
    if (withStub) {$unlistedSessionOwnerRows.set([makeSessionInfo({ id: 'unlisted-draft', profile: 'default', connection_id: 'local' })])}

    const rows = Array.from({ length: 1000 }, (_, i) =>
      makeSessionInfo({ id: `perf-${i}`, profile: 'default', connection_id: 'local' })
    )

    setSessions(rows)

    for (const row of rows) {
      publishSessionState(`rt-${row.id}`, createClientSessionState(row.id))
    }

    for (const row of rows) {
      workSourceAuthorityForSession('local', 'default', row.id)
    }

    const revision = $workSourceAuthorityRevision.get()
    const stream: number[] = []
    const structural: number[] = []

    for (let i = 0; i < 100; i++) {
      const start = performance.now()
      publishSessionState('rt-perf-0', { ...createClientSessionState('perf-0'), streaming: Boolean(i % 2) } as never)
      stream.push(performance.now() - start)
    }

    expect($workSourceAuthorityRevision.get()).toBe(revision)

    for (let i = 0; i < 10; i++) {
      const start = performance.now()
      setSessions(rows.map(row => ({ ...row, title: `title-${i}` })))
      structural.push(performance.now() - start)
    }

    expect($workSourceAuthorityRevision.get()).toBe(revision)
    process.stdout.write(`AUTHORITY_OBSERVER_1000 ${JSON.stringify({ stream, structural })}\n`)
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
