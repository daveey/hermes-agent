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
import { $sessions, setSessions } from '@/store/session'

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

function row(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    ended_at: null,
    id: 's-1',
    input_tokens: 0,
    is_active: false,
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

  it('asks when remote task status is unknown despite zero known counts', async () => {
    setSessions([row({ kanban_unknown: true, kanban_active: 0, kanban_blocked: 0 })])
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
    expect($confirmRequest.get()!.details).toEqual([{ label: 'Rented machines', value: 'vast-4090 (running, $0.40/h)' }])

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
