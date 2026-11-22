import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesApiRequest } from '@/global'
import type { SessionInfo } from '@/hermes'
import { en } from '@/i18n/en'
import { startArchiveTrace } from '@/lib/archive-trace'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $notifications, clearNotifications, dismissNotification, readableError } from '@/store/notifications'

import { commitArchive } from './archive-commit'
import { archivePreflight } from './archive-soft-hide'
import { confirmLinkedArchive } from './confirm-linked-archive'
import { reconnectArchivePreflight, reportDroppedArchiveQueues } from './reconnect-archive-preflight'
import { refreshArchiveTasks } from './refresh-archive-tasks'

const row = { id: 'archive-proof', connection_id: 'owner', profile: 'work', kanban_unknown: true } as SessionInfo
const hidden = () => ({ end: vi.fn(), hid: true, ids: [row.id], restore: vi.fn() })
const upgrade = '409: {"detail":"client_upgrade_required: this app is out of sync (different versions) with the Hermes backend; update the app to edit sessions"}'

beforeEach(() => {
  vi.useFakeTimers()
  sessionStorage.clear()
  localStorage.clear()
  clearNotifications()
})
afterEach(() => {
  settleConfirm(false)
  // Upgrade-required recovery deliberately survives ordinary clearing; acknowledge fixture notices.
  for (const notice of $notifications.get()) {dismissNotification(notice.id)}
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
    expect($notifications.get().some(n => n.message === en.desktop.archiveQueued)).toBe(false)
    expect(Object.keys(sessionStorage)).toHaveLength(0)
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

it('holds a never-recovered queue until the user cancels, without sending any PATCH', async () => {
  const api = vi.fn(async (_request: HermesApiRequest) => {throw new Error('socket hang up')})
  vi.stubGlobal('hermesDesktop', { api })
  const hiddenRow = hidden()
  const result = archivePreflight(row.id, row, true, hiddenRow, en.desktop)
  await vi.advanceTimersByTimeAsync(370_000)
  expect(hiddenRow.restore).not.toHaveBeenCalled()
  $notifications.get().find(n => n.message === en.desktop.archiveQueued)?.action?.onClick()
  expect(await result).toBe(false)
  expect(hiddenRow.restore).toHaveBeenCalledOnce()
  expect($notifications.get().some(n => n.kind === 'error')).toBe(true)
  expect(api.mock.calls.every(([arg]) => !arg.method)).toBe(true)
  expect($confirmRequest.get()).toBeNull()
  expect(Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '')).toHaveLength(0)
})

it('drops reload markers with a visible note, without replay, once; active remounts are not reloads', async () => {
  sessionStorage.setItem('hermes:archive-preflight-queue:old-attempt', row.id)
  const api = vi.fn(async (_request: HermesApiRequest) => {throw new Error('socket hang up')})
  vi.stubGlobal('hermesDesktop', { api })
  await reportDroppedArchiveQueues()
  expect($notifications.get().find(n => n.id === 'archive-queue-reload')?.message).toBe(en.desktop.archiveQueueDropped)
  expect(api).not.toHaveBeenCalled()
  await reportDroppedArchiveQueues()
  expect($notifications.get().filter(n => n.id === 'archive-queue-reload')).toHaveLength(1)
  const result = confirmLinkedArchive(row.id, row, 'work', en.desktop).catch(error => error)
  await vi.advanceTimersByTimeAsync(0)
  const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '')
  expect(keys).toHaveLength(1)
  await reportDroppedArchiveQueues()
  expect(Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index) ?? '')).toEqual(keys)
  await vi.advanceTimersByTimeAsync(70_000)
  $notifications.get().find(n => n.message === en.desktop.archiveQueued)?.action?.onClick()
  await result
})

it.each(['read-failed', 'not-archived', 'wrong-id'])('does not fabricate committed evidence from an ACK and %s verification', async failure => {
  const logLine = vi.fn()
  vi.stubGlobal('hermesDesktop', { logLine, api: async ({ path, method }: HermesApiRequest) => {
    if (path.includes('mutation-snapshot')) {return { exists: true, runtime_revision: 0, runtime_generation: 1 }}

    if (method === 'PATCH') {return { ok: true }}

    if (failure === 'not-archived') {return { id: 'verify-only', archived: false }}

    if (failure === 'wrong-id') {return { id: 'another-session', archived: true }}
    throw new Error('socket hang up')
  } })
  const trace = startArchiveTrace('verify-only')
  expect(await commitArchive({ storedSessionId: 'verify-only', owner: { connectionId: 'owner', profile: 'work' },
    hidden: hidden(), rehide: hidden, copy: en.desktop, onArchived: vi.fn(), retryLabel: 'Retry', trace })).toBe(true)
  // Verification is diagnostic and may finish after the archive ACK has returned.
  await vi.advanceTimersByTimeAsync(0)
  expect(logLine.mock.calls.flat().join('\n')).toContain(`verification-pending:${failure === 'wrong-id' ? 'not-archived' : failure}`)
  expect(logLine.mock.calls.flat().join('\n')).not.toContain(' committed')
})

it('limits a preflight read to the reconnect window remaining budget', async () => {
  let resolveRead!: (value: { id: string }) => void
  const api = vi.fn((_request: HermesApiRequest) => new Promise<{ id: string }>(resolve => {resolveRead = resolve}))
  vi.stubGlobal('hermesDesktop', { api })
  const stages: string[] = []
  const trace = Object.assign((stage: string) => stages.push(stage), { attemptId: 'remaining-budget' })
  let result: SessionInfo | undefined
  let finished = false

  const pending = refreshArchiveTasks(row.id, row, 'work', { budgetMs: 125, trace }).then(value => {
    result = value
    finished = true
  })

  expect(api).toHaveBeenCalledWith(expect.objectContaining({
    path: `/api/sessions/${row.id}`, timeoutMs: 125, connectionId: 'owner', profile: 'work'
  }))
  // The overall timer trails the capped request by a 250ms grace so a transport rejection wins.
  await vi.advanceTimersByTimeAsync(374)
  expect(finished).toBe(false)
  await vi.advanceTimersByTimeAsync(1)
  // Capture the deadline outcome before allowing the read to finish, so a
  // missing budget cap fails without leaving a never-settling test behind.
  const finishedAtDeadline = finished
  const resultAtDeadline = result
  resolveRead({ id: 'mismatched-cleanup-row' })
  await pending
  expect(finishedAtDeadline).toBe(true)
  expect(resultAtDeadline).toMatchObject({ id: row.id, kanban_unknown: true })
  expect(stages).toHaveLength(1)
  expect(stages[0]).toMatch(/^preflight=unknown:no answer within/)
})

it('keeps a timed-out preflight unknown when its successful task read arrives late', async () => {
  let resolveTasks!: (value: { tasks: [] }) => void

  const api = vi.fn(async ({ path }: HermesApiRequest) => {
    if (path === `/api/sessions/${row.id}`) {return { id: row.id }}

    return new Promise<{ tasks: [] }>(resolve => {resolveTasks = resolve})
  })

  vi.stubGlobal('hermesDesktop', { api })
  const stages: string[] = []
  const trace = Object.assign((stage: string) => stages.push(stage), { attemptId: 'late-success' })
  const pending = refreshArchiveTasks(row.id, row, 'work', { trace })
  await vi.advanceTimersByTimeAsync(0)
  expect(api).toHaveBeenCalledTimes(2)
  expect(api.mock.calls[1][0].path).toBe('/api/session-kanban-tasks?ids=archive-proof')
  await vi.advanceTimersByTimeAsync(14_000)
  const result = await pending
  expect(result).toMatchObject({ id: row.id, kanban_unknown: true })
  expect(stages).toHaveLength(1)
  expect(stages[0]).toMatch(/^preflight=unknown:no answer within/)
  const deadlineStages = [...stages]
  resolveTasks({ tasks: [] })
  await vi.advanceTimersByTimeAsync(0)
  expect(stages).toEqual(deadlineStages)
  expect(stages).not.toContain('preflight=ok')
  expect(result?.kanban_unknown).toBe(true)
})

it.each([1, 30])('lets a transport rejection %sms after the capped request timeout beat the overall timer', async (lag: number) => {
  const api = vi.fn(({ timeoutMs }: HermesApiRequest) => new Promise((_resolve, reject) => {
    setTimeout(() => reject(new Error('Timed out connecting to Hermes backend after 5000ms')), (timeoutMs ?? 0) + lag)
  }))

  vi.stubGlobal('hermesDesktop', { api })

  const outcome = refreshArchiveTasks(row.id, row, 'work', { retryTransport: true, budgetMs: 5_000 }).then(
    () => 'resolved', () => 'rejected')

  await vi.advanceTimersByTimeAsync(6_000)
  expect(await outcome).toBe('rejected')
})

it('pins an ownerless preflight to the initial primary across reconnect and a registry switch', async () => {
  $connectionsRegistry.set({ version: 1, primary: 'initial-primary', connections: [], secureTokenStorage: true })
  let disconnected = true

  const api = vi.fn(async ({ path }: HermesApiRequest) => {
    if (disconnected) {disconnected = false; throw new Error('socket hang up')}

    if (path === `/api/sessions/${row.id}`) {return { id: row.id }}

    return { tasks: [] }
  })

  vi.stubGlobal('hermesDesktop', { api })
  const pending = reconnectArchivePreflight(row.id, { ...row, connection_id: undefined }, 'work', en.desktop)
  await vi.advanceTimersByTimeAsync(0)
  expect(api).toHaveBeenCalledOnce()
  expect($notifications.get().some(n => n.message === en.desktop.archiveQueued)).toBe(true)
  $connectionsRegistry.set({ version: 1, primary: 'replacement-primary', connections: [], secureTokenStorage: true })
  await vi.advanceTimersByTimeAsync(1_000)
  expect(await pending).toMatchObject({ connection_id: 'initial-primary', kanban_unknown: false })
  expect(api.mock.calls.map(([request]) => [request.connectionId, request.profile])).toEqual([
    ['initial-primary', 'work'], ['initial-primary', 'work'], ['initial-primary', 'work']
  ])
  expect($notifications.get().some(n => n.message === en.desktop.archiveQueued)).toBe(false)
  expect(Object.keys(sessionStorage)).toHaveLength(0)
})

it('finishes the ACK while diagnostic verification is pending, and never restores or resends on its rejection', async () => {
  let rejectVerification!: (error: Error) => void
  const verification = new Promise<never>((_resolve, reject) => {rejectVerification = reject})
  const logLine = vi.fn()

  const api = vi.fn(async ({ path, method }: HermesApiRequest) => {
    if (path.includes('mutation-snapshot')) {return { exists: true, runtime_revision: 0, runtime_generation: 1 }}

    if (method === 'PATCH') {return { ok: true }}

    expect(path).toBe('/api/sessions/pending-verification')

    return verification
  })

  vi.stubGlobal('hermesDesktop', { api, logLine })
  const hiddenRow = hidden()
  const onArchived = vi.fn()
  let outcome: boolean | undefined

  const pending = commitArchive({ storedSessionId: 'pending-verification', owner: { connectionId: 'owner', profile: 'work' },
    hidden: hiddenRow, rehide: hidden, copy: en.desktop, onArchived, retryLabel: 'Retry', trace: startArchiveTrace('pending-verification') })
    .then(value => {outcome = value})

  await vi.advanceTimersByTimeAsync(0)
  expect(api.mock.calls.filter(([request]) => request.path === '/api/sessions/pending-verification' && !request.method)).toHaveLength(1)

  const beforeDiagnosticFailure = { outcome, ended: hiddenRow.end.mock.calls.length,
    archived: onArchived.mock.calls.length, restored: hiddenRow.restore.mock.calls.length }

  expect(logLine.mock.calls.flat().join('\n')).not.toContain(' committed')
  rejectVerification(new Error('socket hang up'))
  await vi.advanceTimersByTimeAsync(70_000)
  await pending
  expect(beforeDiagnosticFailure).toEqual({ outcome: true, ended: 1, archived: 1, restored: 0 })
  expect(outcome).toBe(true)
  expect(hiddenRow.end).toHaveBeenCalledOnce()
  expect(onArchived).toHaveBeenCalledOnce()
  expect(hiddenRow.restore).not.toHaveBeenCalled()
  expect(api.mock.calls.filter(([request]) => request.method === 'PATCH')).toHaveLength(1)
  expect(api).toHaveBeenCalledTimes(3)
  expect(api.mock.calls.every(([request]) => request.connectionId === 'owner' && request.profile === 'work')).toBe(true)
  expect(logLine.mock.calls.flat().join('\n')).toContain('verification-pending:read-failed')
  expect(logLine.mock.calls.flat().join('\n')).not.toContain(' committed')
  expect($notifications.get().some(n => n.kind === 'error')).toBe(false)
})
