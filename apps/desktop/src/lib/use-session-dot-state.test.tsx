import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { useSessionDotState } from '@/lib/use-session-dot-state'
import { $backgroundStatusBySession } from '@/store/composer-status'
import { $kanbanObservationTick } from '@/store/kanban-observation'
import { $projectTree } from '@/store/project-tree'
import { $cronSessions, $messagingSessions, $sessions, $unlistedSessionOwnerRows, _resetSessionOwnerHintsForTests } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

const NOW = 1001
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW * 1000)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  $backgroundStatusBySession.set({})
  $sessions.set([])
  $cronSessions.set([])
  $messagingSessions.set([])
  $projectTree.set([])
  $unlistedSessionOwnerRows.set([])
  _resetSessionOwnerHintsForTests({ storage: true })
})

// Moved intact from SessionStatusDot's former local-color tests: activity subscriptions still matter,
// but are not canonical status authority and no longer need to be mounted through a status dot.
describe('useSessionDotState owner-row activity subscriptions', () => {
  const ownerRowStores: Array<[string, (row: SessionInfo) => void]> = [
    ['$cronSessions', row => $cronSessions.set([row])],
    ['$messagingSessions', row => $messagingSessions.set([row])],
    [
      '$projectTree',
      row => $projectTree.set([
        { id: 'p', label: 'p', path: null, previewSessions: [row], repos: [], sessionCount: 1 } as SidebarProjectTree
      ])
    ],
    ['$unlistedSessionOwnerRows', row => $unlistedSessionOwnerRows.set([row])]
  ]

  for (const [name, publish] of ownerRowStores) {
    it(`re-evaluates the row when ${name} names the owner of a running process on it`, () => {
      const tick = $kanbanObservationTick.get()
      const id = `proc-${name.slice(1)}`
      const owned = { id, kanban_observed_at: NOW, message_count: 4, profile: 'owner-elsewhere' } as SessionInfo
      $backgroundStatusBySession.set({ [id]: [{ id: 'job', state: 'running', title: 'job', type: 'background' }] })
      const { result } = renderHook(() => useSessionDotState(id, owned))
      expect(result.current).toBe('idle')
      act(() => publish({
        _lineage_root_id: id, id: `${id}-tip`, kanban_observed_at: NOW, message_count: 4, profile: 'owner-elsewhere'
      } as SessionInfo))
      expect($kanbanObservationTick.get()).toBe(tick)
      expect(result.current).toBe('background')
    })
  }
})
