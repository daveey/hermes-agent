import { afterEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $connectionsRegistry } from '@/store/connection-registry-state'

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

it('bounds a hung task read instead of hanging archive', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('hermesDesktop', { api: () => new Promise(() => {}) })
  const pending = confirmLinkedArchive('tip', { id: 'tip', connection_id: 'owner' } as SessionInfo, 'work', en.desktop)
  await vi.advanceTimersByTimeAsync(2500)
  expect($confirmRequest.get()?.details).toEqual([{ label: 'Kanban tasks', value: en.desktop.archiveLinkedTasksUnknown }])
  expect($confirmRequest.get()?.description).not.toMatch(/still has work linked/)
  settleConfirm(false)
  await expect(pending).resolves.toBe(false)
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
