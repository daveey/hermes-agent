// [daveey] Archiving a chat that still has linked work (open Kanban tasks,
// rented machines reported by a plugin guard) asks first; a chat with nothing
// linked archives straight away, as before.
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestProfile as setRequestProfile } from '@/api/client'
import { getSession, type SessionInfo, setSessionArchived } from '@/hermes'
import { registerArchiveGuard, resetArchiveGuardsForTests } from '@/store/archive-guards'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pinnedSessionIds } from '@/store/layout'
import { $notifications } from '@/store/notifications'
import { $offlineSessions, _resetOfflineSessionsForTests } from '@/store/offline-sessions'
import { $projectTree } from '@/store/project-tree'
import { $sessions, _resetSessionOwnerHintsForTests, setSessionOwnerHint, setSessions } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'

import type { ClientSessionState } from '../../../types'

import { useSessionActions } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

const { hubRequest } = vi.hoisted(() => ({ hubRequest: vi.fn(async () => ({ id: 'queued', state: 'local' })) }))
vi.mock('@/store/hub-sync', () => ({ hubRequest }))

function row(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    ended_at: null,
    id: 's-1',
    input_tokens: 0,
    is_active: false,
    kanban_observed_at: Date.now() / 1000,
    last_active: 1,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: 'desktop',
    started_at: 1,
    title: 'GPU sweep',
    tool_call_count: 0,
    ...overrides
  } as SessionInfo
}

type Handle = Pick<ReturnType<typeof useSessionActions>, 'archiveSession'>

function Harness({ onReady }: { onReady: (handle: Handle) => void }) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway: vi.fn().mockResolvedValue(undefined),
    resetViewSync: vi.fn(),
    routedSessionId: null,
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady({ archiveSession: actions.archiveSession })
  }, [actions, onReady])

  return null
}

async function mountHarness(): Promise<Handle> {
  let handle: Handle | undefined
  render(<Harness onReady={h => (handle = h)} />)
  await waitFor(() => expect(handle).toBeDefined())

  return handle!
}

describe('archiveSession linked-work confirmation', () => {
  beforeEach(() => {
    _resetOfflineSessionsForTests()
    hubRequest.mockClear()
    setSessions([])
    resetArchiveGuardsForTests()
    $confirmRequest.set(null)
    vi.mocked(setSessionArchived).mockReset()
    vi.mocked(setSessionArchived).mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    settleConfirm(false)
    cleanup()
    setSessions([])
    resetArchiveGuardsForTests()
  })

  it('archives a chat with nothing linked without asking (control)', async () => {
    setSessions([row()])
    const handle = await mountHarness()

    await act(() => handle.archiveSession('s-1'))

    expect($confirmRequest.get()).toBeNull()
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('s-1', true, { profile: 'default' }, expect.any(Function))
  })

  it('asks before archiving a chat with open Kanban tasks; Cancel keeps it', async () => {
    setSessions([row({ kanban_active: 2, kanban_blocked: 1 })])
    const handle = await mountHarness()

    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1')
    })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())

    const request = $confirmRequest.get()!
    expect(request.title).toBe('Archive “GPU sweep”?')
    expect(request.details).toEqual([{ label: 'Kanban tasks', value: '2 open · 1 blocked' }])

    await act(async () => {
      settleConfirm(false)
      await done
    })

    expect(vi.mocked(setSessionArchived)).not.toHaveBeenCalled()
    expect($sessions.get().map(session => session.id)).toEqual(['s-1'])
  })

  it.each([
    { kanban_unknown: true },
    { kanban_observed_at: undefined },
    { kanban_observed_at: 1 }
  ])('asks when remote task status is unknown despite zero known counts: %j', async evidence => {
    setSessions([row({ ...evidence, kanban_active: 0, kanban_blocked: 0 })])
    const handle = await mountHarness()
    let done: Promise<void> | undefined
    act(() => { done = handle.archiveSession('s-1') })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())
    expect($confirmRequest.get()!.details).toEqual([
      { label: 'Kanban tasks', value: 'Task status unavailable; work may still be unfinished.' }
    ])
    await act(async () => { settleConfirm(false); await done })
    expect(vi.mocked(setSessionArchived)).not.toHaveBeenCalled()
  })

  it('archives after the user confirms', async () => {
    setSessions([row({ kanban_blocked: 1 })])
    const handle = await mountHarness()

    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1')
    })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())
    expect($confirmRequest.get()!.details).toEqual([{ label: 'Kanban tasks', value: '1 blocked' }])

    await act(async () => {
      settleConfirm(true)
      await done
    })

    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('s-1', true, { profile: 'default' }, expect.any(Function))
    expect($sessions.get()).toEqual([])
  })

  it('lists what a plugin guard reports (machines), with the row context it was given', async () => {
    setSessions([row({ _lineage_root_id: 'root-0', connection_id: 'mini', id: 's-1' } as Partial<SessionInfo>)])
    const guard = vi.fn().mockResolvedValue([{ label: 'Rented machines', value: 'vast-4090 (running, $0.40/h)' }])
    registerArchiveGuard('plugin:session-tasks', guard)
    const handle = await mountHarness()

    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1')
    })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())

    expect(guard).toHaveBeenCalledWith(
      expect.objectContaining({ connectionId: 'mini', ids: ['s-1', 'root-0'], profile: 'default', sessionId: 's-1' })
    )
    expect($confirmRequest.get()!.details).toEqual([
      { label: 'Rented machines', value: 'vast-4090 (running, $0.40/h)' }
    ])

    await act(async () => {
      settleConfirm(false)
      await done
    })
    expect(vi.mocked(setSessionArchived)).not.toHaveBeenCalled()
  })

  // [daveey] The row leaves the sidebar the moment archive is clicked, not
  // after the preflight reads (seconds on a busy/remote backend) and the RPC.
  it('hides the row before a slow preflight and archive RPC settle', async () => {
    setSessions([row()])

    let releaseGuard: (links: never[]) => void = () => {}
    registerArchiveGuard('plugin:slow', () => new Promise(resolve => (releaseGuard = resolve)))

    let releaseRpc: (value: { ok: boolean }) => void = () => {}
    vi.mocked(setSessionArchived).mockImplementation(() => new Promise(resolve => (releaseRpc = resolve)))
    const handle = await mountHarness()

    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1')
    })

    expect($sessions.get()).toEqual([])
    expect(vi.mocked(setSessionArchived)).not.toHaveBeenCalled()

    await act(async () => {
      releaseGuard([])
      await waitFor(() => expect(vi.mocked(setSessionArchived)).toHaveBeenCalled())
      releaseRpc({ ok: true })
      await done
    })

    expect($sessions.get()).toEqual([])
  })

  it('hides the row while the linked-work dialog is open; Cancel brings it back', async () => {
    setSessions([row({ kanban_active: 1 })])
    const handle = await mountHarness()

    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1')
    })

    expect($sessions.get()).toEqual([])
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())
    expect($sessions.get()).toEqual([])

    await act(async () => {
      settleConfirm(false)
      await done
    })

    expect($sessions.get().map(session => session.id)).toEqual(['s-1'])
  })

  it('brings the row back when the archive RPC fails', async () => {
    setSessions([row()])
    vi.mocked(setSessionArchived).mockRejectedValue(new Error('boom'))
    const handle = await mountHarness()

    await act(() => handle.archiveSession('s-1'))

    expect($sessions.get().map(session => session.id)).toEqual(['s-1'])
  })

  it('Cancel puts back only the archived pin, keeping pin edits made meanwhile', async () => {
    setSessions([row({ kanban_active: 1 })])
    $pinnedSessionIds.set(['s-1', 'old'])
    const handle = await mountHarness()

    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1')
    })

    expect($pinnedSessionIds.get()).toEqual(['old'])
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())
    $pinnedSessionIds.set(['new'])

    await act(async () => {
      settleConfirm(false)
      await done
    })

    expect($pinnedSessionIds.get()).toEqual(['s-1', 'new'])
    $pinnedSessionIds.set([])
  })

  it('skipLinkedConfirm archives without asking', async () => {
    setSessions([row({ kanban_active: 3 })])
    const handle = await mountHarness()

    await act(() => handle.archiveSession('s-1', { skipLinkedConfirm: true }))

    expect($confirmRequest.get()).toBeNull()
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalled()
  })
})

describe('offline archive owner isolation', () => {
  beforeEach(() => {
    $removedSessionIds.set(new Set())
    _resetOfflineSessionsForTests()
    hubRequest.mockClear()
    setSessions([])
    resetArchiveGuardsForTests()
    $confirmRequest.set(null)
    vi.mocked(setSessionArchived).mockClear()
  })
  afterEach(() => {
    settleConfirm(false)
    cleanup()
    setSessions([])
    _resetOfflineSessionsForTests()
  })

  const cached = (connection_id = 'mini', profile = 'work') =>
    row({
      connection_id,
      profile,
      offline_cached: true,
      offline_gateway: connection_id,
      offline_version: 7
    })

  it.each([
    ['cloud', 'work'],
    ['mini', 'personal']
  ])('archives selected owner %s/%s', async (gateway, profile) => {
    const selectedRow = cached(gateway, profile)
    const otherOwnerRow = cached()
    $offlineSessions.set([otherOwnerRow, selectedRow])
    const handle = await mountHarness()
    await act(() => handle.archiveSession('s-1', { selectedRow }))
    expect(hubRequest).toHaveBeenCalledWith(expect.objectContaining({ gateway, key: profile + '/s-1' }))
    expect($offlineSessions.get()).toEqual([otherOwnerRow])
    expect(setSessionArchived).not.toHaveBeenCalled()
  })
  it('uses live owner after reconnect with the cache still populated', async () => {
    const selectedRow = cached()
    $offlineSessions.set([selectedRow])
    setSessions([{ ...selectedRow, offline_cached: false }])
    const handle = await mountHarness()
    await act(() => handle.archiveSession('s-1', { selectedRow }))
    expect(hubRequest).not.toHaveBeenCalled()
    expect(setSessionArchived).toHaveBeenCalledWith('s-1', true, { connectionId: 'mini', profile: 'work' }, expect.any(Function))
  })
  // Offline rows keep main's owner-scoped flow: no bare-id tombstone, no live-slice copy.
  it('does not tombstone another owner\'s cached chat with the same id', async () => {
    const selectedRow = cached('cloud', 'work')
    const otherOwnerRow = cached('mini', 'personal')
    $offlineSessions.set([selectedRow, otherOwnerRow])
    const handle = await mountHarness()
    await act(() => handle.archiveSession('s-1', { selectedRow }))
    expect($offlineSessions.get()).toEqual([otherOwnerRow])
    expect($removedSessionIds.get().has('s-1')).toBe(false)
  })
  it('keeps a failed offline queue row cached and out of the live list', async () => {
    const selectedRow = cached()
    $offlineSessions.set([selectedRow])
    hubRequest.mockRejectedValueOnce(new Error('hub down'))
    const handle = await mountHarness()
    await act(() => handle.archiveSession('s-1', { selectedRow }))
    expect($removedSessionIds.get().has('s-1')).toBe(false)
    expect($offlineSessions.get()).toEqual([selectedRow])
    expect($sessions.get()).toEqual([])
  })
  it('keeps linked-work confirmation for an offline row', async () => {
    const selectedRow = { ...cached(), kanban_active: 2 }
    $offlineSessions.set([selectedRow])
    const handle = await mountHarness()
    let done: Promise<void> | undefined
    act(() => {
      done = handle.archiveSession('s-1', { selectedRow })
    })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())
    await act(async () => {
      settleConfirm(false)
      await done
    })
    expect(hubRequest).not.toHaveBeenCalled()
    expect($offlineSessions.get()).toEqual([selectedRow])
    expect($sessions.get()).toEqual([])
  })
})

// A remote (hub) chat archived from a door with no selected row — the
// `session.archive` hotkey or a tile's tab menu — while the hub's pooled
// backend reconnects: the unified Recents list carries none of the hub's rows.
// The preflight used to read the registry primary ("local"), whose expected
// 404 is never logged, and showed "Task status unavailable" + "Session
// ancestry unavailable" for a chat with nothing linked.
describe('archive preflight follows the remote owner when Recents lacks it', () => {
  const remoteRow = (overrides: Partial<SessionInfo> = {}) =>
    row({ connection_id: 'hermes-hub', id: 'hub-1', kanban_observed_at: undefined, profile: 'default', ...overrides })

  const api = vi.fn()

  // Mirrors main's hermes:api: the hub knows hub-1; the primary 404s.
  const desktopApi = (hub: 'up' | 'down') => async ({ connectionId, path }: { connectionId?: string; path: string }) => {
    if (connectionId !== 'hermes-hub') {
      throw new Error('404: {"detail":"Session not found"}')
    }

    if (hub === 'down') {
      throw new Error('Timed out connecting to Hermes backend after 2000ms')
    }

    if (path === '/api/sessions/hub-1') {
      return { id: 'hub-1', parent_session_id: null }
    }

    if (path.startsWith('/api/session-kanban-tasks?ids=hub-1')) {
      return { tasks: [] }
    }

    throw new Error(`unexpected ${path}`)
  }

  // The session-tasks plugin's guard contract: read ancestry on the owner it
  // is handed (omitted = registry primary).
  const ancestryGuard = vi.fn(async ({ connectionId, sessionId }: { connectionId?: string; sessionId: string }) => {
    try {
      await api({ connectionId: connectionId ?? 'local', path: `/api/sessions/${sessionId}` })

      return []
    } catch {
      return [{ label: 'Unverified work', value: 'Session ancestry unavailable; linked work or rented machines may still be active.' }]
    }
  })

  beforeEach(() => {
    _resetOfflineSessionsForTests()
    _resetSessionOwnerHintsForTests()
    setSessions([])
    $projectTree.set([])
    resetArchiveGuardsForTests()
    registerArchiveGuard('plugin:session-tasks', ancestryGuard)
    $connectionsRegistry.set({ connections: [], primary: 'local', secureTokenStorage: true, version: 1 })
    $confirmRequest.set(null)
    api.mockReset()
    ancestryGuard.mockClear()
    vi.stubGlobal('hermesDesktop', { api })
    vi.mocked(setSessionArchived).mockReset()
    vi.mocked(setSessionArchived).mockResolvedValue({ ok: true })
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    settleConfirm(false)
    cleanup()
    setSessions([])
    $projectTree.set([])
    _resetSessionOwnerHintsForTests()
    $connectionsRegistry.set(null)
    resetArchiveGuardsForTests()
    vi.unstubAllGlobals()
    vi.mocked(console.error).mockRestore()
  })

  const hub = { connectionId: 'hermes-hub', profile: 'default' }

  it.each([
    ['only the project tree lists it', () => $projectTree.set([{ previewSessions: [remoteRow()], repos: [] } as never])],
    ['Recents holds an ownerless copy', () => {
      setSessions([remoteRow({ connection_id: undefined })])
      $projectTree.set([{ previewSessions: [remoteRow()], repos: [] } as never])
    }],
    ['no list has a row but its owner is remembered', () => setSessionOwnerHint('hub-1', hub)],
    // The sidebar paints the chat from the hub-sync replica (outside the first
    // Recents page, or while the hub's pooled backend reconnects) and no owner
    // hint was ever recorded on this Desktop.
    ['only the sidebar hub-replica row lists it', () =>
      $offlineSessions.set([remoteRow({ offline_cached: false, offline_gateway: 'hub', offline_version: 7 })])]
  ])('archives an idle hub chat without a warning when %s', async (_case, seed) => {
    seed()
    api.mockImplementation(desktopApi('up'))
    const handle = await mountHarness()

    await act(() => handle.archiveSession('hub-1'))

    expect($confirmRequest.get()).toBeNull()
    expect(api.mock.calls.map(([request]) => request.connectionId)).toEqual(['hermes-hub', 'hermes-hub', 'hermes-hub', 'hermes-hub'])
    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'hermes-hub', sessionId: 'hub-1' }))
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('hub-1', true, hub, expect.any(Function))
  })

  it('holds an unreachable hub archive past five minutes and commits on recovery without a second action', async () => {
    $projectTree.set([{ previewSessions: [remoteRow()], repos: [] } as never])
    api.mockImplementation(desktopApi('down'))
    const handle = await mountHarness()
    vi.useFakeTimers()
    let done: Promise<void> | undefined

    act(() => {
      done = handle.archiveSession('hub-1')
    })
    await act(() => vi.advanceTimersByTimeAsync(0))
    expect($confirmRequest.get()).toBeNull()
    expect($notifications.get().some(n => n.message.includes('Will archive when reconnected'))).toBe(true)
    expect(ancestryGuard).not.toHaveBeenCalled()
    expect(api.mock.calls.every(([request]) => request.connectionId === 'hermes-hub')).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(370_000)
      expect($notifications.get().some(n => n.message.includes('Will archive when reconnected'))).toBe(true)
      expect(vi.mocked(setSessionArchived)).not.toHaveBeenCalled()
      expect($confirmRequest.get()).toBeNull()
      api.mockImplementation(desktopApi('up'))
      await vi.advanceTimersByTimeAsync(10_000)
      await done
    })
    vi.useRealTimers()
    expect($notifications.get().some(n => n.kind === 'error')).toBe(false)
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledOnce()
  })

  it('still reports the repair card itself as linked open work', async () => {
    $projectTree.set([{ previewSessions: [remoteRow()], repos: [] } as never])
    api.mockImplementation(async (request: { connectionId?: string; path: string }) =>
      request.path.startsWith('/api/session-kanban-tasks')
        ? { tasks: [{ id: 't_fix', status: 'running' }] }
        : desktopApi('up')(request)
    )
    const handle = await mountHarness()
    let done: Promise<void> | undefined

    act(() => {
      done = handle.archiveSession('hub-1')
    })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())
    expect($confirmRequest.get()!.details).toEqual([{ label: 'Kanban tasks', value: '1 open' }])
    await act(async () => {
      settleConfirm(false)
      await done
    })
  })

  it('keeps an explicitly selected untagged row on the primary (control)', async () => {
    const selectedRow = remoteRow({ connection_id: undefined, kanban_observed_at: Date.now() / 1000 })
    setSessions([selectedRow])
    $projectTree.set([{ previewSessions: [remoteRow()], repos: [] } as never])
    api.mockImplementation(async () => ({ id: 'hub-1', parent_session_id: null }))
    const handle = await mountHarness()

    await act(() => handle.archiveSession('hub-1', { selectedRow }))

    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'local' }))
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('hub-1', true, { connectionId: 'local', profile: 'default' }, expect.any(Function))
  })

  it('pins the invocation active profile for an unlisted unresolved ownerless chat in safety reads and PATCH', async () => {
    setRequestProfile('work')
    vi.mocked(getSession).mockRejectedValue(new Error('socket hang up'))

    let release: (value: unknown) => void = () => {}
    api.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
      .mockImplementation(async ({ path }) => path.includes('session-kanban-tasks') ? { tasks: [] } : { id: 'unlisted', archived: true })
    const handle = await mountHarness()
    let done: Promise<void> | undefined
    act(() => { done = handle.archiveSession('unlisted') })
    await waitFor(() => expect(api).toHaveBeenCalled())
    setRequestProfile('other')
    await act(async () => { release({ id: 'unlisted' }); await done })
    expect(api.mock.calls.map(([request]) => [request.connectionId, request.profile])).toEqual([
      ['local', 'work'], ['local', 'work'], ['local', undefined], ['local', 'work']
    ])
    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'local', profile: 'work' }))
    expect(setSessionArchived).toHaveBeenCalledWith('unlisted', true, { connectionId: 'local', profile: 'work' }, expect.any(Function))
    setRequestProfile(null)
    vi.mocked(getSession).mockReset()
  })

  it('captures the invocation profile before a pending profile resolution settles', async () => {
    setRequestProfile('work')

    let rejectSession: (error: Error) => void = () => {}
    vi.mocked(getSession).mockImplementation(() => new Promise((_resolve, reject) => { rejectSession = reject }))
    api.mockImplementation(async ({ path }: { path: string }) => path.includes('session-kanban-tasks') ? { tasks: [] } : { id: 'unlisted', archived: true })
    const handle = await mountHarness()
    let done: Promise<void> | undefined
    act(() => { done = handle.archiveSession('unlisted') })
    await waitFor(() => expect(getSession).toHaveBeenCalled())
    setRequestProfile('other')
    await act(async () => { rejectSession(new Error('socket hang up')); await done })
    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'local', profile: 'work' }))
    expect(setSessionArchived).toHaveBeenCalledWith('unlisted', true, { connectionId: 'local', profile: 'work' }, expect.any(Function))
    setRequestProfile(null)
    vi.mocked(getSession).mockReset()
  })

  it('keeps default for a row that carries its own connection but no profile, whatever profile is active', async () => {
    setRequestProfile('work')
    vi.mocked(getSession).mockRejectedValue(new Error('socket hang up'))
    $projectTree.set([{ previewSessions: [remoteRow({ profile: undefined })], repos: [] } as never])
    api.mockImplementation(desktopApi('up'))
    const handle = await mountHarness()
    await act(() => handle.archiveSession('hub-1'))
    expect(api.mock.calls.map(([request]) => request.profile)).not.toContain('work')
    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'hermes-hub', profile: 'default' }))
    expect(setSessionArchived).toHaveBeenCalledWith('hub-1', true, hub, expect.any(Function))
    setRequestProfile(null)
    vi.mocked(getSession).mockReset()
  })

  it('writes to the resolved nondefault profile, not default, for an ownerless/profileless row', async () => {
    const selectedRow = remoteRow({ connection_id: undefined, profile: undefined, kanban_observed_at: Date.now() / 1000 })
    setSessions([selectedRow])
    vi.mocked(getSession).mockResolvedValueOnce({ ...selectedRow, profile: 'work' })
    api.mockImplementation(async () => ({ id: 'hub-1', archived: true }))
    const handle = await mountHarness()
    await act(() => handle.archiveSession('hub-1', { selectedRow }))
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('hub-1', true,
      { connectionId: 'local', profile: 'work' }, expect.any(Function))
    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'local', profile: 'work' }))
  })
})
