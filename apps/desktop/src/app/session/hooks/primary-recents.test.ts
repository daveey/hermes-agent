import { describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import type { SessionInfo, SidebarSessionsResponse } from '@/hermes'

import { mergePrimaryRecents, ownRecentsConnection, withPrimaryRecents } from './primary-recents'

const row = (id: string, over: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 3, profile: 'default', title: id, ...over }) as SessionInfo

const page = (
  sessions: SessionInfo[],
  extra: Partial<SidebarSessionsResponse['recents']> = {}
): SidebarSessionsResponse => ({
  cron: { sessions: [] },
  messaging: { sessions: [] },
  recents: { sessions, ...extra }
})

describe('withPrimaryRecents', () => {
  it('asks nothing extra while the window is on the primary (or untagged)', async () => {
    const fetchPrimary = vi.fn()

    for (const windowConnection of [null, undefined, '', 'local']) {
      const result = await withPrimaryRecents(
        Promise.resolve(page([row('a')])),
        windowConnection,
        'local',
        fetchPrimary
      )

      expect(result.recents.sessions.map(s => s.id)).toEqual(['a'])
    }

    expect(fetchPrimary).not.toHaveBeenCalled()
  })

  it("adds this Mac's own stamped rows, tagged with the primary, while the window is on another gateway", async () => {
    const hub = row('h1', { connection_id: 'hermes-hub' })
    const own = row('cron-chat', { cron_active: true })

    const result = await withPrimaryRecents(Promise.resolve(page([hub])), 'hermes-hub', 'local', () =>
      Promise.resolve(page([own]))
    )

    expect(result.recents.sessions).toEqual([hub, { ...own, connection_id: 'local' }])
  })

  it("keeps the window's page when the primary read fails or comes back failed", async () => {
    const hub = page([row('h1', { connection_id: 'hermes-hub' })])

    const rejected = await withPrimaryRecents(Promise.resolve(hub), 'hermes-hub', 'local', () =>
      Promise.reject(new Error('ECONNREFUSED'))
    )

    expect(rejected).toBe(hub)
    expect(mergePrimaryRecents(hub, page([row('x')], { failed: true }), 'local')).toBe(hub)
    expect(mergePrimaryRecents(hub, page([row('x')], { retry: true }), 'local')).toBe(hub)
    expect(mergePrimaryRecents(hub, page([]), 'local')).toBe(hub)
  })

  it('never duplicates a row the window page already carries for the same owner', () => {
    const hub = page([row('same', { connection_id: 'local' }), row('h1', { connection_id: 'hermes-hub' })])
    const merged = mergePrimaryRecents(hub, page([row('same'), row('h1')]), 'local')

    // `h1` of the primary is a different owner than the hub's `h1`: both stay; `same` stays once.
    expect(merged.recents.sessions.map(s => [s.id, s.connection_id])).toEqual([
      ['same', 'local'],
      ['h1', 'hermes-hub'],
      ['h1', 'local']
    ])
  })
})

describe('ownRecentsConnection', () => {
  const registry = (primary: string, kind: 'local' | 'ssh'): DesktopConnectionsRegistry =>
    ({ connections: [{ id: primary, kind, label: primary }], primary, version: 1 }) as DesktopConnectionsRegistry

  it('names the registry primary, not the window primary socket that moved to the hub', () => {
    expect(ownRecentsConnection(registry('local', 'local'), 'hermes-hub')).toEqual({
      connectionId: 'local',
      passive: true
    })
  })

  it('reads a remote registry primary actively', () => {
    expect(ownRecentsConnection(registry('mini', 'ssh'), 'hermes-hub')).toEqual({
      connectionId: 'mini',
      passive: false
    })
  })

  it('falls back to the window primary without a registry (legacy topology)', () => {
    expect(ownRecentsConnection(null, 'local')).toEqual({ connectionId: 'local', passive: true })
    expect(ownRecentsConnection(null, 'hermes-hub')).toEqual({ connectionId: 'hermes-hub', passive: false })
  })
})
