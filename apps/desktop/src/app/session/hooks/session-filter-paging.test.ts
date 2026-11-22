// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import { $sessionsLimit, resetSidebarView } from '@/store/layout'
import { SESSION_FILTER_AREA } from '@/store/session-filter'

import { useSessionListActions } from './use-session-list-actions'

const listSidebarSessions = vi.hoisted(() => vi.fn())
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listSidebarSessions
}))
vi.mock('../../cron/cron-actions', () => ({ refreshCronJobs: vi.fn() }))

afterEach(() => {
  cleanup()
  resetSidebarView()
  $sessionsLimit.set(50)
})

it('deepens the page for a plugin-only filter and restores it on disposal', async () => {
  resetSidebarView()
  $sessionsLimit.set(50)
  listSidebarSessions.mockResolvedValue({
    recents: { sessions: [], total: 0 },
    cron: { sessions: [], total: 0 },
    messaging: { sessions: [], total: 0 }
  })
  renderHook(() => useSessionListActions({ profileScope: 'default' }))

  let dispose = () => {}

  try {
    await act(async () => {
      dispose = registry.register({ area: SESSION_FILTER_AREA, id: 'paging-lane', data: { match: () => true } })
    })
    expect($sessionsLimit.get()).toBe(300)
    expect(listSidebarSessions).toHaveBeenCalledTimes(1)

    // React effect cleanup + setup replaces the contribution in one tick.
    // Selection/session-map updates must not shrink and reload the page.
    for (let update = 0; update < 2; update++) {
      await act(async () => {
        dispose()
        dispose = registry.register({ area: SESSION_FILTER_AREA, id: 'paging-lane', data: { match: () => false } })
        expect($sessionsLimit.get()).toBe(300)
      })
      expect($sessionsLimit.get()).toBe(300)
      expect(listSidebarSessions).toHaveBeenCalledTimes(1)
    }

    await act(async () => dispose())
    expect($sessionsLimit.get()).toBe(50)
    expect(listSidebarSessions).toHaveBeenCalledTimes(2)
  } finally {
    act(() => dispose())
  }
})
