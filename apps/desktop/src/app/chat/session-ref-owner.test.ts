import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $sessions, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $sessionTiles, type SessionTile } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { registryConnectionIdForGateway, resolveSessionRefOwner } from './session-ref-owner'

const row = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

const registry = () =>
  ({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: 'skint-polycentrism', kind: 'cloud', label: 'Nous' },
      { id: 'mac-mini', kind: 'ssh', label: 'Mac Mini' }
    ],
    primary: 'local',
    secureTokenStorage: true,
    version: 2
  }) as never

describe('resolveSessionRefOwner', () => {
  beforeEach(() => {
    _resetSessionOwnerHintsForTests()
    $connectionsRegistry.set(registry())
    $sessions.set([])
    $sessionTiles.set([])
  })

  afterEach(() => {
    $connectionsRegistry.set(null)
    $sessions.set([])
    $sessionTiles.set([])
  })

  it('routes a gateway-qualified ref to that gateway by slugged label or id', () => {
    expect(resolveSessionRefOwner('default@mac-mini/s1')).toEqual({
      connectionId: 'mac-mini',
      profile: 'default',
      targetProfile: 'default'
    })
    expect(resolveSessionRefOwner('work@nous/s1')).toEqual({
      connectionId: 'skint-polycentrism',
      profile: 'work',
      targetProfile: 'work'
    })
    expect(registryConnectionIdForGateway('skint-polycentrism')).toBe('skint-polycentrism')
  })

  it('leaves an unknown gateway qualifier unrouted (ambient fallback)', () => {
    expect(resolveSessionRefOwner('default@nowhere/s1')).toBeUndefined()
  })

  it('routes an unqualified ref to the tagged unified-list row that owns the id', () => {
    $sessions.set([row({ connection_id: 'mac-mini', id: 's1', profile: 'default' })])

    expect(resolveSessionRefOwner('default/s1')).toEqual({
      connectionId: 'mac-mini',
      profile: 'default',
      targetProfile: 'default'
    })
  })

  it('routes an untagged unified-list row to the registry primary (the reported blank tab)', () => {
    // Live shape with the cloud gateway selected: the local session's row in the
    // unified list carries NO connection_id; only foreign rows are tagged.
    $sessions.set([
      row({ connection_id: 'skint-polycentrism', id: 'remote1', profile: 'default' }),
      row({ id: '20261014_011122_5e31ed', profile: 'default' })
    ])

    expect(resolveSessionRefOwner('default/20261014_011122_5e31ed')).toEqual({
      connectionId: 'local',
      profile: 'default',
      targetProfile: 'default'
    })
  })

  it('leaves an untagged row unrouted without registry topology (single-gateway installs)', () => {
    $connectionsRegistry.set(null)
    $sessions.set([row({ id: 's1', profile: 'default' })])

    expect(resolveSessionRefOwner('default/s1')).toBeUndefined()
  })

  it('ignores a tagged row on a different profile than the ref names', () => {
    $sessions.set([row({ connection_id: 'mac-mini', id: 's1', profile: 'work' })])

    expect(resolveSessionRefOwner('default/s1')).toBeUndefined()
  })

  it('falls back to the remembered owner hint', () => {
    setSessionOwnerHint('s1', { connectionId: 'mac-mini', profile: 'default' })

    expect(resolveSessionRefOwner('default/s1')).toMatchObject({ connectionId: 'mac-mini' })
  })

  it('treats an unqualified, unknown ref as living on the gateway of the chat that shows it', () => {
    $sessionTiles.set([
      { ownerRoute: { connectionId: 'local', profile: 'default' }, storedSessionId: 'origin' } as SessionTile
    ])

    expect(resolveSessionRefOwner('default/s1', 'origin')).toEqual({
      connectionId: 'local',
      profile: 'default',
      targetProfile: 'default'
    })
  })

  it('returns undefined when nothing is known (keeps the ambient behaviour)', () => {
    expect(resolveSessionRefOwner('default/s1', 'origin')).toBeUndefined()
    expect(resolveSessionRefOwner('')).toBeUndefined()
  })
})
