// [daveey] Archiving a chat that still has linked work (open Kanban tasks,
// rented machines reported by a plugin guard) asks first; a chat with nothing
// linked archives straight away, as before.
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type SessionInfo, setSessionArchived } from '@/hermes'
import { registerArchiveGuard, resetArchiveGuardsForTests } from '@/store/archive-guards'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $offlineSessions, _resetOfflineSessionsForTests } from '@/store/offline-sessions'
import { $projectTree } from '@/store/project-tree'
import { $sessions, _resetSessionOwnerHintsForTests, setSessionOwnerHint, setSessions } from '@/store/session'

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
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('s-1', true, 'default')
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

    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('s-1', true, 'default')
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
    expect(setSessionArchived).toHaveBeenCalledWith('s-1', true, { connectionId: 'mini', profile: 'work' })
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
    ['no list has a row but its owner is remembered', () => setSessionOwnerHint('hub-1', hub)]
  ])('archives an idle hub chat without a warning when %s', async (_case, seed) => {
    seed()
    api.mockImplementation(desktopApi('up'))
    const handle = await mountHarness()

    await act(() => handle.archiveSession('hub-1'))

    expect($confirmRequest.get()).toBeNull()
    expect(api.mock.calls.map(([request]) => request.connectionId)).toEqual(['hermes-hub', 'hermes-hub', 'hermes-hub'])
    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'hermes-hub', sessionId: 'hub-1' }))
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('hub-1', true, hub)
  })

  it('still warns when the owning hub is genuinely unreachable', async () => {
    $projectTree.set([{ previewSessions: [remoteRow()], repos: [] } as never])
    api.mockImplementation(desktopApi('down'))
    const handle = await mountHarness()
    let done: Promise<void> | undefined

    act(() => {
      done = handle.archiveSession('hub-1')
    })
    await waitFor(() => expect($confirmRequest.get()).not.toBeNull())

    expect($confirmRequest.get()!.details).toEqual([
      { label: 'Kanban tasks', value: 'Task status unavailable; work may still be unfinished.' },
      { label: 'Unverified work', value: 'Session ancestry unavailable; linked work or rented machines may still be active.' }
    ])
    expect(api.mock.calls.every(([request]) => request.connectionId === 'hermes-hub')).toBe(true)
    expect(console.error).toHaveBeenCalledWith(expect.stringMatching(/^\[archive-preflight\] task status unknown for hub-1 via hermes-hub\/default: GET \/api\/sessions\/hub-1 failed: Timed out/))
    await act(async () => {
      settleConfirm(false)
      await done
    })
    expect(vi.mocked(setSessionArchived)).not.toHaveBeenCalled()
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

    expect(ancestryGuard).toHaveBeenCalledWith(expect.objectContaining({ connectionId: undefined }))
    expect(vi.mocked(setSessionArchived)).toHaveBeenCalledWith('hub-1', true, 'default')
  })
})
