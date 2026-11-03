import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SessionRefChip, SessionRefLink } from '@/components/assistant-ui/directive-text'
import { __resetSessionLinkTitleCache } from '@/lib/session-link-title'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $sessions, _resetSessionOwnerHintsForTests } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { transcriptRestScope } from '../session/hooks/use-session-actions/provisional-transcript'

import { resolveSessionRefOwner } from './session-ref-owner'
import {
  prefetchSessionRef,
  REF_PREFETCH_MIN_MS,
  resetSessionRefPrefetch,
  sessionRefPrefetchKey
} from './session-ref-prefetch'

const row = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

const registry = () =>
  ({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: 'hermes-hub', kind: 'ssh', label: 'hermes-hub' },
      { id: 'mac-mini', kind: 'ssh', label: 'Mac Mini' }
    ],
    primary: 'local',
    secureTokenStorage: true,
    version: 2
  }) as never

// Let the chip's lazy import and its effect run.
const settle = () => act(() => new Promise(resolve => setTimeout(resolve, 20)))

let prefetch: ReturnType<typeof vi.fn>
let previousDesktop: unknown

beforeEach(() => {
  resetSessionRefPrefetch()
  _resetSessionOwnerHintsForTests()
  $connectionsRegistry.set(registry())
  $sessions.set([])
  $sessionTiles.set([])
  prefetch = vi.fn()
  previousDesktop = (window as any).hermesDesktop
  ;(window as any).hermesDesktop = { ...(previousDesktop as object), transcriptCache: { prefetch } }
})

afterEach(() => {
  cleanup()
  ;(window as any).hermesDesktop = previousDesktop
  $connectionsRegistry.set(null)
  $sessions.set([])
  $sessionTiles.set([])
  __resetSessionLinkTitleCache()
})

describe('@session ref chips warm the transcript cache', () => {
  it('render + pointer-enter issues exactly one prefetch for the resolved owner key', async () => {
    $sessions.set([row({ connection_id: 'hermes-hub', id: '20261018_114650_7ed013', profile: 'default' })])

    render(<SessionRefChip value="default/20261018_114650_7ed013" />)
    await waitFor(() => expect(prefetch).toHaveBeenCalledTimes(1))

    fireEvent.pointerEnter(screen.getByTitle('default/20261018_114650_7ed013'))
    await settle()

    expect(prefetch).toHaveBeenCalledTimes(1)
    expect(prefetch).toHaveBeenCalledWith({
      connectionId: 'hermes-hub',
      profile: 'default',
      sessionId: '20261018_114650_7ed013'
    })
  })

  it('a second render within MIN_REFETCH_MS issues none; after the window it sends again', async () => {
    $sessions.set([row({ connection_id: 'hermes-hub', id: 's1', profile: 'default' })])

    render(<SessionRefChip value="default/s1" />)
    await waitFor(() => expect(prefetch).toHaveBeenCalledTimes(1))
    cleanup()

    render(<SessionRefLink value="default/s1" />)
    await settle()
    fireEvent.pointerEnter(screen.getByTitle('default/s1'))
    await settle()

    expect(prefetch).toHaveBeenCalledTimes(1)

    // The throttle is per window, not forever.
    expect(prefetchSessionRef('default/s1', null, Date.now() + REF_PREFETCH_MIN_MS + 1)).toEqual({
      connectionId: 'hermes-hub',
      profile: 'default',
      sessionId: 's1'
    })
    expect(prefetch).toHaveBeenCalledTimes(2)
  })

  it('an unresolved owner issues none, then a hover after the owner is known sends it', async () => {
    render(<SessionRefChip value="default/unknown1" />)
    await settle()
    fireEvent.pointerEnter(screen.getByTitle('default/unknown1'))
    await settle()

    expect(prefetch).not.toHaveBeenCalled()

    // Not remembered as sent: once the list knows the owner, a hover warms it.
    $sessions.set([row({ connection_id: 'mac-mini', id: 'unknown1', profile: 'default' })])
    fireEvent.pointerEnter(screen.getByTitle('default/unknown1'))
    await waitFor(() => expect(prefetch).toHaveBeenCalledTimes(1))
    expect(prefetch).toHaveBeenCalledWith({ connectionId: 'mac-mini', profile: 'default', sessionId: 'unknown1' })
  })

  it('a gateway-qualified ref targets that gateway (markdown link)', async () => {
    // A row on another gateway with the same id must not win over the qualifier.
    $sessions.set([row({ connection_id: 'hermes-hub', id: 's9', profile: 'work' })])

    render(<SessionRefLink value="work@mac-mini/s9" />)
    await waitFor(() => expect(prefetch).toHaveBeenCalledTimes(1))

    expect(prefetch).toHaveBeenCalledWith({ connectionId: 'mac-mini', profile: 'work', sessionId: 's9' })
  })

  it('an unknown gateway qualifier issues none', async () => {
    render(<SessionRefLink value="default@nowhere/s9" />)
    await settle()

    expect(prefetch).not.toHaveBeenCalled()
  })

  it('the warmed key is exactly the scope the open reads its cache paint from', () => {
    $sessions.set([row({ connection_id: 'hermes-hub', id: 's1', profile: 'work' })])

    for (const value of ['work/s1', 'work@mac-mini/s1']) {
      const owner = resolveSessionRefOwner(value)
      const key = sessionRefPrefetchKey(value)

      expect(owner).toBeDefined()
      expect({ connectionId: key!.connectionId, profile: key!.profile }).toEqual(
        transcriptRestScope(owner, undefined, '')
      )
    }
  })
})
