import { SIDEBAR_SECTIONS_AREA } from '@hermes/plugin-sdk'
import { describe, expect, it, vi } from 'vitest'

import plugin from './plugin'

// [daveey] The hub-sync Desktop half is a service now: cached sessions flow
// into the ordinary sidebar list; it contributes no sidebar section of its own.

const api = vi.hoisted(() => ({
  cursor: 1 as null | number,
  loads: 0,
  published: [] as unknown[][],
  rows: [] as unknown[] | null
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => ({
  ...(await importOriginal<object>()),
  publishOfflineSessions: (rows: unknown[]) => void api.published.push(rows)
}))

vi.mock('./api', () => ({
  loadReplicaSidebarRows: vi.fn(async () => {
    api.loads++

    return api.rows
  }),
  replicaCursor: vi.fn(async () => api.cursor)
}))

function fakeContext() {
  const registered: Array<{ area: string }> = []
  const disposers: Array<() => void> = []

  const ctx = {
    onDispose: (fn: () => void) => void disposers.push(fn),
    register: (entry: { area: string }) => void registered.push(entry),
    registerMany: (entries: Array<{ area: string }>) => void registered.push(...entries),
    storage: { get: (_key: string, fallback: unknown) => fallback, set: vi.fn() }
  }

  return { ctx, disposers, registered }
}

describe('hub-sync plugin', () => {
  it('registers no standalone "Synced from other gateways" sidebar section', () => {
    const { ctx, disposers, registered } = fakeContext()

    plugin.register(ctx as never)

    expect(registered.filter(entry => entry.area === SIDEBAR_SECTIONS_AREA)).toEqual([])
    disposers.forEach(dispose => dispose())
  })
})

describe('hub-sync plugin: replica cursor polling (instant loads)', () => {
  it('reloads rows only when the replica cursor moves, and at least every 30 s', async () => {
    vi.useFakeTimers()

    try {
      api.cursor = 1
      api.loads = 0
      const { ctx, disposers } = fakeContext()
      plugin.register(ctx as never)
      await vi.advanceTimersByTimeAsync(0)
      expect(api.loads).toBe(1) // the initial load

      await vi.advanceTimersByTimeAsync(2_000) // first tick learns cursor 1
      const afterFirst = api.loads
      await vi.advanceTimersByTimeAsync(6_000) // unchanged cursor: no reloads
      expect(api.loads).toBe(afterFirst)

      api.cursor = 2
      await vi.advanceTimersByTimeAsync(2_000)
      expect(api.loads).toBe(afterFirst + 1)

      await vi.advanceTimersByTimeAsync(30_000) // the periodic full reload re-evaluates owner freshness
      expect(api.loads).toBeGreaterThan(afterFirst + 1)
      disposers.forEach(dispose => dispose())
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('hub-sync plugin: a failed replica read publishes nothing (cold start)', () => {
  it('publishes nothing while the read has no answer, and publishes once it does', async () => {
    vi.useFakeTimers()

    try {
      api.published = []
      api.rows = null // backend still starting: hub-sync answers nothing
      const { ctx, disposers } = fakeContext()
      plugin.register(ctx as never)
      await vi.advanceTimersByTimeAsync(0)

      expect(api.published).toEqual([]) // the rows already showing (boot snapshot / last poll) stay

      api.rows = [{ id: 's_mini' }]
      api.cursor = 99
      await vi.advanceTimersByTimeAsync(2_000)

      expect(api.published).toEqual([[{ id: 's_mini' }]])
      disposers.forEach(dispose => dispose())
    } finally {
      api.rows = []
      vi.useRealTimers()
    }
  })
})
