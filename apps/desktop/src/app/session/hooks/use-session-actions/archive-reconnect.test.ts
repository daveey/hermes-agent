import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesApiRequest } from '@/global'
import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { startArchiveTrace } from '@/lib/archive-trace'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $notifications, clearNotifications, readableError } from '@/store/notifications'

import { commitArchive } from './archive-commit'
import { archivePreflight } from './archive-soft-hide'
import { confirmLinkedArchive } from './confirm-linked-archive'
import { reportDroppedArchiveQueues } from './reconnect-archive-preflight'

const row = { id: 'archive-proof', connection_id: 'owner', profile: 'work', kanban_unknown: true } as SessionInfo
const hidden = () => ({ end: vi.fn(), hid: true, ids: [row.id], restore: vi.fn() })
const upgrade = '409: {"detail":"client_upgrade_required: this app is out of sync (different versions) with the Hermes backend; update the app to edit sessions"}'

beforeEach(() => {
  vi.useFakeTimers()
  sessionStorage.clear()
  clearNotifications()
})
afterEach(() => {
  settleConfirm(false)
  clearNotifications()
  $connectionsRegistry.set(null)
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

async function archive(trace: ReturnType<typeof startArchiveTrace>) {
  if (!(await confirmLinkedArchive(row.id, row, 'work', en.desktop, false, trace))) {return false}

  return commitArchive({ storedSessionId: row.id, owner: { connectionId: 'owner', profile: 'work' },
    hidden: hidden(), rehide: hidden, copy: en.desktop, onArchived: vi.fn(), retryLabel: 'Retry', trace })
}

it.each(['direct', 'preflight reconnect', 'snapshot reconnect', 'PATCH reconnect', 'upgrade refusal'])('correlates %s with one start, real sends and authoritative commit only', async mode => {
  const stages: string[] = []
  let archived = false
  let failed = false
  const patches: Record<string, unknown>[] = []

  const api = vi.fn(async ({ path, method, body, connectionId, profile }) => {
    expect([connectionId, profile]).toEqual(['owner', 'work'])

    if (path.includes('mutation-snapshot')) {
      if (mode === 'snapshot reconnect' && !failed) {failed = true; throw new Error('socket hang up')}

      return { exists: true, runtime_revision: 0, runtime_generation: 1 }
    }

    if (method === 'PATCH') {
      patches.push(body)

      if (mode === 'upgrade refusal') {throw new Error(upgrade)}

      if (mode === 'PATCH reconnect' && !failed) {failed = true; throw new Error('socket hang up')}
      archived = true

      return { ok: true }
    }

    if (path.startsWith('/api/sessions/')) {
      if (mode === 'preflight reconnect' && !failed) {failed = true; throw new Error('Timed out connecting to Hermes backend after 6000ms')}

      return { id: row.id, parent_session_id: null, archived }
    }

    return { tasks: [] }
  })

  vi.stubGlobal('hermesDesktop', { api, logLine: (line: string) => {
    if (line.startsWith('[archive-trace]')) {stages.push(line)}
  } })
  const trace = startArchiveTrace(row.id)
  const result = archive(trace)
  await vi.advanceTimersByTimeAsync(0)

  if (mode === 'preflight reconnect') {
    expect($notifications.get().some(n => n.message === en.desktop.archiveQueued && n.persistent)).toBe(true)
    expect(patches).toHaveLength(0)
  }

  await vi.advanceTimersByTimeAsync(1_000)
  expect(await result).toBe(mode !== 'upgrade refusal')
  const prefix = `[archive-trace] ${row.id} ${trace.attemptId} `
  const events = stages.map(line => line.slice(prefix.length))
  expect(stages.every(line => line.startsWith(prefix))).toBe(true)
  expect(events.filter(e => e === 'start')).toHaveLength(1)
  expect(events[0]).toBe('start')
  expect(events).toContain('preflight=ok')
  expect(events).toContain('patch-sent n=1')

  if (mode === 'preflight reconnect') {
    expect(events.slice(1, 4)).toEqual(['preflight=waiting-reconnect', 'queued', 'preflight=ok'])
  }

  if (mode === 'PATCH reconnect') {
    expect(events).toContain('patch-sent n=2')
    expect(patches[0].request_id).toBeTruthy()
    expect(patches[1]).toEqual(patches[0])
  }

  if (mode === 'upgrade refusal') {
    expect(events.at(-1)).toMatch(/^failed:.*client_upgrade_required/)
    expect(events).not.toContain('committed')
    expect(patches).toHaveLength(1)
    expect(readableError(new Error(upgrade), 'Archive failed')).toMatchObject({
      message: 'The app and the backend are on different versions. Update both.', action: { label: 'Update Hermes' }
    })
  } else {
    expect(events.at(-1)).toBe('committed')
  }
})

it('reruns ancestry and task safety after a reconnect, never treating unknown as permission', async () => {
  let failed = false
  const paths: string[] = []
  vi.stubGlobal('hermesDesktop', { api: vi.fn(async ({ path }) => {
    paths.push(path)

    if (path === `/api/sessions/${row.id}`) {return { id: row.id, parent_session_id: 'root' }}

    if (path === '/api/sessions/root') {return { id: 'root' }}

    if (!failed) {failed = true; throw new Error('Remote backend "conn:owner::work" is down (socket hang up); reconnect in progress')}

    return { tasks: [{ id: 'new-task', status: 'running' }] }
  }) })
  const pending = confirmLinkedArchive(row.id, row, 'work', en.desktop)
  await vi.advanceTimersByTimeAsync(0)
  $connectionsRegistry.set({ version: 1, primary: 'different', connections: [], secureTokenStorage: true })
  await vi.advanceTimersByTimeAsync(1_000)
  expect(paths).toEqual([`/api/sessions/${row.id}`, '/api/sessions/root', '/api/session-kanban-tasks?ids=archive-proof%2Croot',
    `/api/sessions/${row.id}`, '/api/sessions/root', '/api/session-kanban-tasks?ids=archive-proof%2Croot'])
  expect($confirmRequest.get()?.details).toEqual([{ label: 'Kanban tasks', value: '1 open' }])
  settleConfirm(false)
  expect(await pending).toBe(false)
})

it('ends a never-recovered queue visibly without sending any PATCH', async () => {
  const api = vi.fn(async (_request: HermesApiRequest) => {throw new Error('socket hang up')})
  vi.stubGlobal('hermesDesktop', { api })
  const hiddenRow = hidden()
  const result = archivePreflight(row.id, row, true, hiddenRow, en.desktop)
  await vi.advanceTimersByTimeAsync(70_000)
  expect(await result).toBe(false)
  expect(hiddenRow.restore).toHaveBeenCalledOnce()
  expect($notifications.get().some(n => n.kind === 'error')).toBe(true)
  expect(api.mock.calls.every(([arg]) => !arg.method)).toBe(true)
  expect($confirmRequest.get()).toBeNull()
  expect(Object.keys(sessionStorage)).toHaveLength(0)
})

it('drops reload markers with a visible note, without replay, once; active remounts are not reloads', async () => {
  sessionStorage.setItem('hermes:archive-preflight-queue:old-attempt', row.id)
  const api = vi.fn(async (_request: HermesApiRequest) => {throw new Error('socket hang up')})
  vi.stubGlobal('hermesDesktop', { api })
  reportDroppedArchiveQueues()
  expect($notifications.get().find(n => n.id === 'archive-queue-reload')?.message).toBe(en.desktop.archiveQueueDropped)
  expect(api).not.toHaveBeenCalled()
  reportDroppedArchiveQueues()
  expect($notifications.get().filter(n => n.id === 'archive-queue-reload')).toHaveLength(1)
  const result = confirmLinkedArchive(row.id, row, 'work', en.desktop).catch(error => error)
  await vi.advanceTimersByTimeAsync(0)
  const keys = Object.keys(sessionStorage)
  expect(keys).toHaveLength(1)
  reportDroppedArchiveQueues()
  expect(Object.keys(sessionStorage)).toEqual(keys)
  await vi.advanceTimersByTimeAsync(70_000)
  await result
})

it.each(['read-failed', 'not-archived'])('does not fabricate committed evidence from an ACK and %s verification', async failure => {
  const logLine = vi.fn()
  vi.stubGlobal('hermesDesktop', { logLine, api: async ({ path, method }: HermesApiRequest) => {
    if (path.includes('mutation-snapshot')) {return { exists: true, runtime_revision: 0, runtime_generation: 1 }}

    if (method === 'PATCH') {return { ok: true }}

    if (failure === 'not-archived') {return { id: 'verify-only', archived: false }}
    throw new Error('socket hang up')
  } })
  const trace = startArchiveTrace('verify-only')
  expect(await commitArchive({ storedSessionId: 'verify-only', owner: { connectionId: 'owner', profile: 'work' },
    hidden: hidden(), rehide: hidden, copy: en.desktop, onArchived: vi.fn(), retryLabel: 'Retry', trace })).toBe(true)
  expect(logLine.mock.calls.flat().join('\n')).toContain(`verification-pending:${failure}`)
  expect(logLine.mock.calls.flat().join('\n')).not.toContain(' committed')
})
