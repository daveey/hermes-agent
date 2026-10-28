import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import {
  $bootSessions,
  $bootSnapshot,
  $sidebarFallbackSessions,
  _resetBootSnapshotForTests,
  applyBootSnapshot,
  markSidebarFirstRows,
  markSidebarLive,
  saveBootTree
} from './boot-snapshot'
import { $connectionsRegistry } from './connection-registry-state'
import { rememberHubGatewayName } from './hub-sync'
import {
  $offlineSessions,
  _resetOfflineSessionsForTests,
  mergeOfflineFallback,
  recordSessionListResult
} from './offline-sessions'
import { $projectTree } from './project-tree'

// [daveey] Cold start: the first window paints the sidebar from the boot snapshot (hub.db rows of every gateway, this
// one's included, plus the last projects tree) before the backend is up; live lists replace it per owner.

const NOW_S = 1_800_000_000

const record = (gateway: string, id: string, extra: Record<string, unknown> = {}) => ({
  activity: NOW_S - 30,
  gateway,
  id,
  key: `default/${id}`,
  local: gateway === 'macbook',
  message_count: 4,
  profile: 'default',
  source: 'desktop',
  stale: false,
  synced_at: NOW_S - 5,
  title: `Chat ${id}`,
  version: 1,
  ...extra
})

const registry = () =>
  ({
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      { id: 'mac-mini', kind: 'ssh', label: 'Mac Mini' }
    ],
    primary: 'local',
    version: 2
  }) as never

const snapshot = (over: Record<string, unknown> = {}) => ({
  appStartMs: 1_000,
  hub: {
    available: true as const,
    gateway: 'macbook',
    sessions: [record('macbook', 's_own'), record('mini', 's_mini', { running: true })],
    stale: false,
    synced_at: NOW_S - 5
  },
  readMs: 12,
  tree: null,
  ...over
})

beforeEach(() => {
  _resetBootSnapshotForTests()
  _resetOfflineSessionsForTests()
  $projectTree.set([])
  $connectionsRegistry.set(null)
  localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('applyBootSnapshot', () => {
  it("shows this gateway's own rows at once, under the replica connection, never as offline", () => {
    applyBootSnapshot(() => snapshot(), NOW_S * 1000)
    const own = $bootSessions.get().find(row => row.id === 's_own')!

    expect($bootSnapshot.get().painted).toBe(true)
    expect(own.connection_id).toBe('local')
    expect(own.offline_cached).toBe(false)
    expect(own.title).toBe('Chat s_own')
  })

  it("shows another gateway's rows once the registry can map its hub name, with its running state", () => {
    applyBootSnapshot(() => snapshot(), NOW_S * 1000)
    expect($bootSessions.get().map(row => row.id)).toEqual(['s_own']) // no registry yet: mini is unmapped

    $connectionsRegistry.set(registry())
    rememberHubGatewayName('mac-mini', 'mini')
    $connectionsRegistry.set(registry())
    const mini = $bootSessions.get().find(row => row.id === 's_mini')!

    expect(mini.connection_id).toBe('mac-mini')
    expect(mini.is_active).toBe(true)
  })

  it('marks an asleep owner (no publish in the asleep window) as cached and never running', () => {
    const asleep = snapshot({
      hub: { ...snapshot().hub, sessions: [record('mini', 's_old', { running: true, synced_at: NOW_S - 3600 })] }
    })

    $connectionsRegistry.set(registry())
    rememberHubGatewayName('mac-mini', 'mini')
    applyBootSnapshot(() => asleep, NOW_S * 1000)
    const row = $bootSessions.get()[0]

    expect(row.offline_cached).toBe(true)
    expect(row.is_active).toBe(false)
  })

  it('seeds the projects tree only while it is empty', () => {
    applyBootSnapshot(
      () => snapshot({ tree: { activeProjectId: null, connectionId: 'local', savedAt: 1, tree: [{ id: 'p_saved' }] } }),
      NOW_S * 1000
    )
    expect($projectTree.get()).toEqual([{ id: 'p_saved' }])
    expect($bootSnapshot.get().treeProjects).toBe(1)

    _resetBootSnapshotForTests()
    $projectTree.set([{ id: 'p_live' }] as never)
    applyBootSnapshot(
      () => snapshot({ tree: { activeProjectId: null, connectionId: 'local', savedAt: 1, tree: [{ id: 'p_saved' }] } }),
      NOW_S * 1000
    )
    expect($projectTree.get()).toEqual([{ id: 'p_live' }])
  })

  it('paints nothing without the bridge, an empty snapshot, or a failing read', () => {
    expect(applyBootSnapshot(() => undefined).painted).toBe(false)
    expect(applyBootSnapshot(() => snapshot({ hub: null, tree: null })).painted).toBe(false)
    expect(
      applyBootSnapshot(() => {
        throw new Error('ipc down')
      }).painted
    ).toBe(false)
    expect($bootSessions.get()).toEqual([])
  })
})

describe('boot rows in the sidebar fallback', () => {
  it('a plugin row of the same identity wins over the boot row', () => {
    applyBootSnapshot(() => snapshot(), NOW_S * 1000)
    const pluginRow = { ...$bootSessions.get()[0], title: 'From the plugin poll' } as SessionInfo
    $offlineSessions.set([pluginRow])

    const fallback = $sidebarFallbackSessions.get()

    expect(fallback.filter(row => row.id === 's_own').map(row => row.title)).toEqual(['From the plugin poll'])
  })

  it("drops this gateway's boot rows once its own live list is fresh", () => {
    applyBootSnapshot(() => snapshot(), NOW_S * 1000)

    // Before any live list: the boot rows are the sidebar.
    expect(mergeOfflineFallback([], $sidebarFallbackSessions.get()).map(row => row.id)).toEqual(['s_own'])

    const live = [{ id: 's_new', profile: 'default', title: 'Live chat' } as SessionInfo]
    recordSessionListResult([], live, undefined, 'default')

    expect(mergeOfflineFallback(live, $sidebarFallbackSessions.get()).map(row => row.id)).toEqual(['s_new'])
  })
})

describe('saveBootTree', () => {
  it('saves the tree debounced, with no running state, for the next cold start', () => {
    vi.useFakeTimers()

    const saveTree = vi.fn()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { bootSnapshot: { read: () => null, saveTree } }
    $projectTree.set([{ id: 'p', previewSessions: [{ id: 's', is_active: true }] }] as never)

    saveBootTree('local', 'p')
    saveBootTree('local', 'p')
    expect(saveTree).not.toHaveBeenCalled()

    vi.advanceTimersByTime(3_000)
    expect(saveTree).toHaveBeenCalledTimes(1)
    expect(saveTree.mock.calls[0][0]).toEqual({
      activeProjectId: 'p',
      connectionId: 'local',
      tree: [{ id: 'p', previewSessions: [{ id: 's', is_active: false }] }]
    })
  })

  it('saves the primary as an empty connection id when the owner has none', () => {
    vi.useFakeTimers()

    const saveTree = vi.fn()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { bootSnapshot: { read: () => null, saveTree } }

    saveBootTree(null, null)
    vi.advanceTimersByTime(3_000)

    expect(saveTree.mock.calls[0][0].connectionId).toBe('')
  })
})

describe('[sidebar-timing]', () => {
  it('writes one line with first rows and first live list, measured from process start', () => {
    const logLine = vi.fn()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { logLine }
    applyBootSnapshot(() => snapshot(), NOW_S * 1000)

    markSidebarFirstRows(12, false, 1_450)
    markSidebarFirstRows(40, true, 9_000) // only the first paint counts
    expect(logLine).not.toHaveBeenCalled()

    markSidebarLive(7_200)
    markSidebarLive(8_000)

    expect(logLine).toHaveBeenCalledTimes(1)
    const line = logLine.mock.calls[0][0] as string

    expect(line.startsWith('[sidebar-timing] ')).toBe(true)
    expect(JSON.parse(line.slice('[sidebar-timing] '.length))).toEqual({
      firstRowsMs: 450,
      liveMs: 6_200,
      readMs: 12,
      rows: 12,
      snapshotProjects: 0,
      snapshotRows: 2,
      source: 'snapshot'
    })
  })

  it('records nothing without a process start time (no snapshot bridge)', () => {
    const logLine = vi.fn()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { logLine }

    markSidebarFirstRows(3, true, 5_000)
    markSidebarLive(6_000)

    expect(logLine).not.toHaveBeenCalled()
  })
})
