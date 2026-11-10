/**
 * [daveey] Which surface a `@session` ref open lands on, and what it logs.
 *
 * The 11-10 live proof of the ref cache paint collected two `[open-timing]` lines for the target, both
 * `startedBy:"click"` with no `surface`: sidebar-row main-pane opens. These tests pin the two entry paths so a
 * proof can tell them apart: the chip/link opens a TILE (and never writes a main-pane click record), under the
 * same cache key the chip prefetched; a sidebar click on a chat already open as a tile only fronts that tile and
 * must not leave a dangling click record that later logs as an abandoned open.
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSessionRefPrefetch } from '@/app/chat/session-ref-prefetch'
import { useOfflineFallback } from '@/app/chat/sidebar/use-offline-fallback'
import { openSession } from '@/app/open-session'
import { SessionRefChip } from '@/components/assistant-ui/directive-text'
import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { type OpenTimingRecord, resetOpenTimings } from '@/lib/open-timing'
import { __resetSessionLinkTitleCache } from '@/lib/session-link-title'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $sessions, _resetSessionOwnerHintsForTests } from '@/store/session'
import { $sessionTiles, openSessionTile } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

const TARGET = '20261018_114650_7ed013'
const lines = () => (globalThis as { __hermesOpenTimings?: OpenTimingRecord[] }).__hermesOpenTimings ?? []
const settle = (ms = 30) => act(() => new Promise(resolve => setTimeout(resolve, ms)))

let previousDesktop: unknown
let prefetch: ReturnType<typeof vi.fn>

beforeEach(() => {
  resetOpenTimings()
  resetSessionRefPrefetch()
  _resetSessionOwnerHintsForTests()
  $sessionTiles.set([])
  $layoutTree.set(null)
  $connectionsRegistry.set({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: 'hermes-hub', kind: 'ssh', label: 'hermes-hub' }
    ],
    primary: 'local',
    secureTokenStorage: true,
    version: 2
  } as never)
  $sessions.set([{ connection_id: 'hermes-hub', id: TARGET, profile: 'default', title: 'Old target' } as SessionInfo])
  prefetch = vi.fn()
  previousDesktop = (window as { hermesDesktop?: unknown }).hermesDesktop
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
    ...(previousDesktop as object),
    transcriptCache: { prefetch }
  }
})

afterEach(() => {
  cleanup()
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = previousDesktop
  $connectionsRegistry.set(null)
  $sessions.set([])
  $sessionTiles.set([])
  $layoutTree.set(null)
  __resetSessionLinkTitleCache()
  resetOpenTimings()
})

describe('[daveey] @session ref open surface', () => {
  it('a chip click opens a tile owned by the key the chip prefetched, and writes no main-pane click record', async () => {
    render(<SessionRefChip label="Old target" value={`default/${TARGET}`} />)
    await settle()

    const key = { connectionId: 'hermes-hub', profile: 'default', sessionId: TARGET }
    expect(prefetch).toHaveBeenCalledWith(key)

    fireEvent.click(screen.getByRole('button'))
    await settle(50)

    const tile = $sessionTiles.get().find(candidate => candidate.storedSessionId === TARGET)

    // The tile's cache paint reads (owner.connectionId, owner.targetProfile || owner.profile): the prefetched key.
    expect(tile?.ownerRoute).toMatchObject({ connectionId: key.connectionId, targetProfile: key.profile })
    expect(lines().filter(record => record.startedBy === 'click')).toEqual([])
  })

  it('a sidebar click on a chat already open as a tile fronts the tile and logs no abandoned main-pane open', () => {
    const mainResume = vi.fn()
    // wiring.openStoredSession: an in-place open; main resumes only when it routes there.
    const resumeSession = (id: string) => openSession(id, () => mainResume(id))

    openSessionTile(TARGET, 'center')
    $layoutTree.set(group(['workspace', `session-tile:${TARGET}`], { active: 'workspace', id: 'main' }))

    const { result } = renderHook(() => useOfflineFallback([], resumeSession))

    act(() => result.current.onResumeSession(TARGET, $sessions.get()[0]))
    expect(mainResume).not.toHaveBeenCalled()

    // The next open used to flush the never-resumed click as {"startedBy":"click","outcome":"abandoned"}.
    act(() => result.current.onResumeSession('other', { connection_id: 'hermes-hub', id: 'other' } as SessionInfo))

    expect(lines().filter(record => record.id === TARGET)).toEqual([])
  })
})
