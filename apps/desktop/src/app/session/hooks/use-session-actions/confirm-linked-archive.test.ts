import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $notifications } from '@/store/notifications'

import { confirmLinkedArchive } from './confirm-linked-archive'

afterEach(() => {
  settleConfirm(false)
  $connectionsRegistry.set(null)
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it.each(['offline', 'malformed', 'stale', 'unknown'])('keeps genuine %s task failures uncertain and retries', async failure => {
  const row = { id: 'failure-tip', connection_id: 'owner', profile: 'work', kanban_observed_at: 1 } as SessionInfo
  let recovered = false

  const api = vi.fn(async ({ path }) => {
    if (path.startsWith('/api/sessions/')) {return { id: 'failure-tip', parent_session_id: null }}

    if (recovered) {return { tasks: [] }}

    if (failure === 'offline') {throw new Error('503')}

    if (failure === 'malformed') {return {}}

    return { tasks: [{ id: 'task', status: failure === 'unknown' ? 'unknown' : 'running', stale: failure === 'stale', unfinished: true }] }
  })

  vi.stubGlobal('hermesDesktop', { api })
  const pending = confirmLinkedArchive(row.id, row, 'work', en.desktop)
  await vi.waitFor(() => expect($confirmRequest.get()).not.toBeNull())
  expect($confirmRequest.get()?.details).toEqual([{ label: 'Kanban tasks', value: en.desktop.archiveLinkedTasksUnknown }])
  expect($confirmRequest.get()?.description).not.toMatch(/still has work linked/)
  settleConfirm(false)
  await expect(pending).resolves.toBe(false)
  recovered = true
  await expect(confirmLinkedArchive(row.id, row, 'work', en.desktop)).resolves.toBe(true)
})

it('walks compressed ancestors and preserves linked-task confirmation', async () => {
  const row = { id: 'tip', connection_id: 'owner', profile: 'work', kanban_unknown: true } as SessionInfo

  const api = vi.fn(async ({ path }) => {
    if (path === '/api/sessions/tip') {return { id: 'tip', parent_session_id: 'middle' }}

    if (path === '/api/sessions/middle') {return { id: 'middle', parent_session_id: 'root' }}

    if (path === '/api/sessions/root') {return { id: 'root', parent_session_id: null }}
    expect(path).toBe('/api/session-kanban-tasks?ids=tip%2Cmiddle%2Croot')

    return { tasks: [{ id: 'task', status: 'running' }] }
  })

  vi.stubGlobal('hermesDesktop', { api })
  const pending = confirmLinkedArchive('tip', row, 'work', en.desktop)
  await vi.waitFor(() => expect($confirmRequest.get()).not.toBeNull())
  expect($confirmRequest.get()?.details).toEqual([{ label: 'Kanban tasks', value: '1 open' }])
  settleConfirm(false)
  await expect(pending).resolves.toBe(false)
})

it('bounds each hung task read but holds the intent until cancelled, not an override dialog', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('hermesDesktop', { api: () => new Promise(() => {}) })

  const pending = confirmLinkedArchive('tip', { id: 'tip', connection_id: 'owner' } as SessionInfo, 'work', en.desktop)
    .then(() => 'resolved', error => String(error))

  await vi.advanceTimersByTimeAsync(70_000)
  expect($confirmRequest.get()).toBeNull()
  $notifications.get().find(n => n.message === en.desktop.archiveQueued)?.action?.onClick()
  expect(await pending).toBe(`Error: ${en.desktop.archiveQueueCancelled}`)
})

it.each([undefined, 'remote-owner'])('refreshes expired empty observations on the row owner %s', async connectionId => {
  $connectionsRegistry.set({ version: 1, primary: 'primary-owner', connections: [], secureTokenStorage: true })

  const api = vi.fn(async ({ path }) => path.startsWith('/api/sessions/')
    ? { id: 'tip', parent_session_id: null }
    : { tasks: [] })

  vi.stubGlobal('hermesDesktop', { api })
  const row = { id: 'tip', profile: 'work', connection_id: connectionId, kanban_observed_at: 1 } as SessionInfo
  const pending = confirmLinkedArchive('tip', row, 'work', en.desktop)
  await vi.waitFor(() => expect(api).toHaveBeenCalled())
  await expect(pending).resolves.toBe(true)
  expect($confirmRequest.get()).toBeNull()
  expect(api.mock.calls.every(([args]) => args.connectionId === (connectionId || 'primary-owner') && args.profile === 'work')).toBe(true)
})

it('retries a timed-out read once instead of reporting unknown', async () => {
  const row = { id: 'tip', connection_id: 'owner', profile: 'work', kanban_unknown: true } as SessionInfo
  let sessionCalls = 0

  const api = vi.fn(async ({ path, timeoutMs }) => {
    expect(timeoutMs).toBeGreaterThanOrEqual(5_000)

    if (path.startsWith('/api/sessions/')) {
      if (++sessionCalls === 1) {throw new Error('Timed out connecting to Hermes backend after 6000ms')}

      return { id: 'tip', parent_session_id: null }
    }

    return { tasks: [] }
  })

  vi.stubGlobal('hermesDesktop', { api })
  await expect(confirmLinkedArchive('tip', row, 'work', en.desktop)).resolves.toBe(true)
  expect(sessionCalls).toBe(2)
  expect($confirmRequest.get()).toBeNull()
})

const nowSeconds = () => Date.now() / 1000

it('archives without asking when a fresh row only counts settled cards awaiting close-out', async () => {
  const row = { id: 'settled', connection_id: 'owner', profile: 'work', kanban_active: 7, kanban_observed_at: nowSeconds() } as SessionInfo

  const api = vi.fn(async ({ path }) => path.startsWith('/api/sessions/') ? { id: 'settled', parent_session_id: null } : { tasks: [] })

  vi.stubGlobal('hermesDesktop', { api })
  await expect(confirmLinkedArchive('settled', row, 'work', en.desktop)).resolves.toBe(true)
  expect($confirmRequest.get()).toBeNull()
})

it('asks with the live count, not the row count, when a fresh row claims work', async () => {
  const row = { id: 'mixed', connection_id: 'owner', profile: 'work', kanban_active: 7, kanban_observed_at: nowSeconds() } as SessionInfo

  const api = vi.fn(async ({ path }) => path.startsWith('/api/sessions/')
    ? { id: 'mixed', parent_session_id: null }
    : { tasks: [{ id: 'task', status: 'running' }, { id: 'ask', status: 'blocked' }] })

  vi.stubGlobal('hermesDesktop', { api })
  const pending = confirmLinkedArchive('mixed', row, 'work', en.desktop)
  await vi.waitFor(() => expect($confirmRequest.get()).not.toBeNull())
  expect($confirmRequest.get()?.details).toEqual([{ label: 'Kanban tasks', value: '1 open · 1 blocked' }])
  settleConfirm(false)
  await expect(pending).resolves.toBe(false)
})

it('keeps a fresh row\'s count when the live read fails', async () => {
  const row = { id: 'offline', connection_id: 'owner', profile: 'work', kanban_active: 2, kanban_observed_at: nowSeconds() } as SessionInfo

  vi.stubGlobal('hermesDesktop', { api: vi.fn(async () => { throw new Error('503') }) })
  const pending = confirmLinkedArchive('offline', row, 'work', en.desktop)
  await vi.waitFor(() => expect($confirmRequest.get()).not.toBeNull())
  expect($confirmRequest.get()?.details).toEqual([{ label: 'Kanban tasks', value: '2 open' }])
  settleConfirm(false)
  await expect(pending).resolves.toBe(false)
})
