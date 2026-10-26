import { SIDEBAR_SECTIONS_AREA } from '@hermes/plugin-sdk'
import { describe, expect, it, vi } from 'vitest'

import plugin from './plugin'

// [daveey] The hub-sync Desktop half is a service now: cached sessions flow
// into the ordinary sidebar list; it contributes no sidebar section of its own.

vi.mock('./api', () => ({ loadReplicaSidebarRows: vi.fn(async () => []) }))

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
